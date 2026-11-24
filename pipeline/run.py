"""STEP 4 - ORCHESTRATION.

    python -m pipeline.run                         # weekly run (target = last complete ISO week)
    python -m pipeline.run --run-date 2026-11-23   # pretend today is 2026-11-23
    python -m pipeline.run --backfill-weeks 8      # also (re)load the 8 weeks before the target
    python -m pipeline.run --from-raw              # replay saved raw pages instead of calling the API
    python -m pipeline.run --no-email

extract -> load/transform (upsert + SCD2 + KPI SQL) -> quality gate -> publish
(CSV + Markdown/HTML + charts) -> save state snapshot -> email.

Exit code is 0 on success and 1 on any failure; failures are also emitted as a
GitHub Actions ``::error::`` annotation so they show on the run summary page.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import emailer, extract, quality, report, state, transform
from .config import Paths, RunContext
from .logging_utils import log, setup_logging

logger = logging.getLogger("pipeline.run")


@contextmanager
def step(name: str, **ctx):
    start = time.monotonic()
    log(logger, logging.INFO, "step started", step=name, **ctx)
    try:
        yield
    except Exception:
        log(logger, logging.ERROR, "step failed", step=name, duration_s=round(time.monotonic() - start, 2))
        raise
    log(logger, logging.INFO, "step finished", step=name, duration_s=round(time.monotonic() - start, 2))


def _github_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def _github_error(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=KPI pipeline failed::{clean}", flush=True)


def process_week(con, ctx: RunContext, paths: Paths, from_raw: bool, client=None) -> None:
    with step("extract", week_start=ctx.week_start, week_end=ctx.week_end):
        if from_raw:
            records = extract.load_raw_pages(paths.raw_dir, ctx.run_date)
            log(logger, logging.INFO, "replayed raw pages", studies=len(records))
        else:
            records = extract.extract(ctx, paths.raw_dir, client)
    with step("load_transform"):
        transform.load_and_transform(con, ctx, records)
    with step("quality_checks"):
        quality.enforce(con)


def run(run_date: date, backfill_weeks: int = 0, from_raw: bool = False, send_email: bool = True,
        fresh: bool = False, paths: Paths | None = None, client=None) -> int:
    paths = paths or Paths.from_env()
    target = RunContext.for_run_date(run_date, today=datetime.now(timezone.utc).date())
    weeks = [RunContext.for_week(target.week_start - timedelta(weeks=n)) for n in range(backfill_weeks, 0, -1)]
    weeks.append(target)
    log(logger, logging.INFO, "pipeline started", run_date=run_date, target_week=target.week_start,
        weeks_to_process=len(weeks), target_is_backfill=target.is_backfill)

    with step("restore_state"):
        if fresh and paths.warehouse.exists():
            paths.warehouse.unlink()
        rebuild = not paths.warehouse.exists()
        con = transform.connect(paths.warehouse)
        if rebuild:
            state.restore(con, paths.state_dir)
        else:
            log(logger, logging.INFO, "using existing local warehouse", path=str(paths.warehouse))

    try:
        for ctx in weeks:
            process_week(con, ctx, paths, from_raw, client)
            if ctx is not target:  # backfill weeks only feed the CSV history
                report.append_kpi_history(report.week_trend(con, ctx), paths.kpi_history_csv, ctx)

        with step("publish"):
            result = report.publish(con, target, paths)

        with step("save_state"):
            state.save(con, paths.state_dir)
        _github_output("published", "true")

        with step("email"):
            if send_email:
                subject = f"Weekly Clinical Trials KPI Report - week of {target.week_start:%d %b %Y}"
                images = [(f["cid"], paths.figures_dir / f["file"]) for f in result.report_ctx.figures]
                sent = emailer.send_report(subject, result.email_html, result.markdown_path.read_text("utf-8"), images)
                _github_output("email_sent", str(sent).lower())
            else:
                log(logger, logging.INFO, "email disabled by --no-email")

        log(logger, logging.INFO, "pipeline succeeded", counts=transform.table_counts(con),
            report=str(result.html_path))
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly ClinicalTrials.gov KPI pipeline")
    parser.add_argument("--run-date", type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date(), help="YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--backfill-weeks", type=int, default=0,
                        help="also process this many weeks before the target week (oldest first)")
    parser.add_argument("--from-raw", action="store_true", help="replay data/raw/<run_date>/ instead of the API")
    parser.add_argument("--no-email", action="store_true", help="skip the email step")
    parser.add_argument("--fresh", action="store_true",
                        help="discard the local warehouse and rebuild it from the committed snapshot")
    args = parser.parse_args(argv)

    paths = Paths.from_env()
    setup_logging(paths.log_dir)
    try:
        return run(args.run_date, args.backfill_weeks, args.from_raw, not args.no_email, args.fresh, paths)
    except Exception as exc:  # noqa: BLE001 - top-level guard: log, annotate, exit non-zero
        logger.exception("pipeline failed", extra={"ctx": {"error_type": type(exc).__name__}})
        _github_error(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
