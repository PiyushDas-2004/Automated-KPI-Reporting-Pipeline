"""STEP 3 - PUBLISH.

* Appends this week's KPI rows (with WoW and 4-week rolling averages) to the
  versioned, Power BI-ready ``reports/kpi_history.csv`` (one row per week per
  KPI series; a rerun replaces that week's rows instead of duplicating them).
* Renders ``reports/latest.md`` and ``reports/latest.html`` from Jinja2
  templates, with headline KPIs + WoW arrows, three charts and an
  auto-written "3 things that changed this week" section.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts
from .config import PIPELINE_VERSION, Paths, RunContext
from .logging_utils import log
from .transform import read_sql

logger = logging.getLogger("pipeline.report")

CSV_COLUMNS = [
    "week_start", "week_end", "kpi_name", "dimension", "dimension_value", "value",
    "prev_week_value", "wow_change", "wow_pct", "rolling_4wk_avg", "weeks_in_rolling_avg",
    "run_date", "is_backfill", "pipeline_version",
]

# (kpi_name, label, is_up_good, unit) - None = direction is neutral
HEADLINE_KPIS = [
    ("new_trials", "New trials registered", None, ""),
    ("updated_trials", "Trials with a registry update", None, ""),
    ("status_changes", "Confirmed status changes", None, ""),
    ("stopped_trials", "Updated trials now terminated / withdrawn / suspended", False, ""),
    ("industry_share_new_trials_pct", "Industry-sponsored share of new trials", None, "%"),
    ("median_planned_enrollment", "Median planned enrollment (new trials)", None, ""),
    ("new_trial_sites", "New trial sites", None, ""),
]

# KPIs whose values are counts of trials - eligible for the "biggest movements" z-score.
COUNT_KPIS = {"new_trials", "updated_trials", "status_changes", "stopped_trials"}

SPONSOR_CLASS_LABELS = {
    "INDUSTRY": "industry", "NIH": "NIH", "FED": "US federal", "OTHER_GOV": "other government",
    "NETWORK": "network", "INDIV": "individual", "OTHER": "academic / other", "AMBIG": "ambiguous",
    "UNKNOWN": "unknown",
}


def status_label(status: str) -> str:
    return status.replace("_", " ").lower()


# ---------------------------------------------------------------------------
# CSV dataset
# ---------------------------------------------------------------------------

def week_trend(con: duckdb.DuckDBPyConnection, ctx: RunContext) -> pd.DataFrame:
    return con.execute(
        "SELECT * FROM kpi_trend WHERE week_start = ? ORDER BY kpi_name, dimension, dimension_value",
        [ctx.week_start],
    ).df()


def append_kpi_history(trend: pd.DataFrame, csv_path: Path, ctx: RunContext) -> pd.DataFrame:
    new_rows = trend.copy()
    new_rows["pipeline_version"] = PIPELINE_VERSION
    for col in ("week_start", "week_end", "run_date"):
        new_rows[col] = pd.to_datetime(new_rows[col]).dt.strftime("%Y-%m-%d")
    new_rows = new_rows[CSV_COLUMNS]

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype={"week_start": str})
        existing = existing[existing["week_start"] != ctx.week_start.isoformat()]  # idempotent rerun
        combined = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined = new_rows

    combined = combined.sort_values(["week_start", "kpi_name", "dimension", "dimension_value"])
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(csv_path, index=False)
    log(logger, logging.INFO, "kpi_history.csv updated", rows_this_week=len(new_rows),
        total_rows=len(combined), weeks=combined["week_start"].nunique())
    return combined


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------

def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt(v, unit: str = "") -> str:
    v = _num(v)
    if v is None:
        return "-"
    if unit == "%":
        return f"{v:.1f}%"
    if float(v).is_integer() or abs(v) >= 100:
        return f"{v:,.0f}"
    return f"{v:,.1f}"


@dataclass
class Headline:
    label: str
    value: str
    arrow: str
    delta: str
    tone: str  # good / bad / neutral / none
    rolling: str


def _arrow(change: float | None) -> str:
    if change is None:
        return "–"
    return "▲" if change > 0 else "▼" if change < 0 else "▬"


def build_headlines(trend: pd.DataFrame) -> list[Headline]:
    totals = trend[trend["dimension"] == "total"].set_index("kpi_name")
    out = []
    for kpi, label, up_good, unit in HEADLINE_KPIS:
        if kpi not in totals.index:
            continue
        row = totals.loc[kpi]
        change, pct = _num(row["wow_change"]), _num(row["wow_pct"])
        if change is None:
            delta, tone = "no prior week", "none"
        else:
            delta = f"{change:+,.1f}" if unit == "%" else f"{change:+,.0f}"
            delta += " pts" if unit == "%" else ""
            if pct is not None and unit != "%":
                delta += f" ({pct:+.1f}%)"
            delta += " vs last week"
            if change == 0 or up_good is None:
                tone = "neutral"
            else:
                tone = "good" if (change > 0) == up_good else "bad"
        out.append(Headline(label, fmt(row["value"], unit), _arrow(change), delta, tone,
                            fmt(row["rolling_4wk_avg"], unit)))
    return out


def _subject(kpi: str, dimension: str, value: str) -> str:
    if kpi == "new_trials":
        return {
            "total": "New trial registrations",
            "phase": f"New {value} registrations",
            "sponsor_class": f"New registrations from {SPONSOR_CLASS_LABELS.get(value, value)} sponsors",
            "condition_top10": f"New trials in {value}",
            "study_type": f"New {status_label(value)} studies",
        }.get(dimension, f"New trials ({value})")
    if kpi == "updated_trials":
        return "Registry updates" if dimension == "total" else f"Updated trials now {status_label(value)}"
    if kpi == "status_changes":
        return "Confirmed status changes" if dimension == "total" else f"Confirmed moves to {status_label(value)}"
    if kpi == "stopped_trials":
        return "Stopped trials" if dimension == "total" else f"Stopped trials citing '{value.lower()}'"
    return f"{kpi} ({value})"


def biggest_movements(trend: pd.DataFrame, n: int = 3, min_base: int = 10) -> list[str]:
    """Rank week-over-week moves by a Poisson z-score and describe the top ``n``.

    For counts, the change between two weeks has a standard deviation of about
    sqrt(this + last), so z = (this - last) / sqrt(this + last). This favours
    moves that are both large AND unlikely to be noise (a jump from 2 to 6 is
    +200% but z = 1.4; a jump from 400 to 480 is +20% but z = 2.7).

    Breakdown rows (phase, sponsor class, ...) are *mix-adjusted*: their
    expected value is last week's value scaled by the KPI total's change, so
    if every segment falls 15% because the total fell 15%, the segments are not
    reported as three separate "changes" - only segments that moved
    differently from the overall trend score highly.
    """
    df = trend[trend["kpi_name"].isin(COUNT_KPIS)].copy()
    df = df[df["prev_week_value"].notna()]
    df = df[(df["value"] + df["prev_week_value"]) >= min_base]  # ignore tiny bases
    df = df[df["wow_change"] != 0]
    if df.empty:
        return []

    totals = trend[(trend["dimension"] == "total") & trend["prev_week_value"].notna()]
    ratio = {r.kpi_name: (r.value / r.prev_week_value) for r in totals.itertuples() if r.prev_week_value}
    total_pct = {r.kpi_name: _num(r.wow_pct) for r in totals.itertuples()}

    def score(r) -> float:
        expected = r["prev_week_value"]
        if r["dimension"] != "total":
            expected *= ratio.get(r["kpi_name"], 1.0)
        return (r["value"] - expected) / ((r["value"] + expected) ** 0.5 or 1.0)

    df["z"] = df.apply(score, axis=1)
    df = df.sort_values("z", key=lambda s: s.abs(), ascending=False)
    df = df.drop_duplicates(subset=["kpi_name", "dimension"])  # one item per breakdown

    lines = []
    for _, r in df.head(n).iterrows():
        verb = "rose" if r["wow_change"] > 0 else "fell"
        pct = f" {abs(r['wow_pct']):.0f}%" if _num(r["wow_pct"]) is not None else ""
        avg = _num(r["rolling_4wk_avg"])
        if avg is not None and r["weeks_in_rolling_avg"] >= 2:
            rel = "above" if r["value"] > avg * 1.05 else "below" if r["value"] < avg * 0.95 else "in line with"
            ctx = f", {rel} the 4-week average of {fmt(avg)}"
        else:
            ctx = ""
        overall = total_pct.get(r["kpi_name"])
        if r["dimension"] != "total" and overall is not None:
            ctx += f" (overall: {overall:+.0f}%)"
        lines.append(
            f"{_subject(r['kpi_name'], r['dimension'], r['dimension_value'])} {verb}{pct} week over week "
            f"({fmt(r['prev_week_value'])} → {fmt(r['value'])}){ctx}."
        )
    return lines


def composition_facts(trend: pd.DataFrame) -> list[str]:
    """Fallback statements when there is no prior week to compare against."""
    facts = []

    def rows(kpi, dim):
        return trend[(trend["kpi_name"] == kpi) & (trend["dimension"] == dim)].sort_values("value", ascending=False)

    phases = rows("new_trials", "phase")
    phased = phases[~phases["dimension_value"].isin(["Not applicable", "Observational / other"])]
    if not phased.empty and phased["value"].sum() > 0:
        top = phased.iloc[0]
        facts.append(f"{top['dimension_value']} led phased registrations with {fmt(top['value'])} new trials "
                     f"({top['value'] / phased['value'].sum():.0%} of phase 1-4 trials).")
    countries = rows("new_trial_sites", "country_top10")
    if not countries.empty:
        top = countries.iloc[0]
        facts.append(f"{top['dimension_value']} added the most new trial sites ({fmt(top['value'])}).")
    reasons = rows("stopped_trials", "stop_reason")
    if not reasons.empty and reasons["value"].sum() > 0:
        top = reasons.iloc[0]
        facts.append(f"The most common reason for stopping a trial was '{top['dimension_value'].lower()}' "
                     f"({fmt(top['value'])} of {fmt(reasons['value'].sum())} stopped trials).")
    return facts


def things_that_changed(trend: pd.DataFrame) -> list[str]:
    lines = biggest_movements(trend)
    for fact in composition_facts(trend):
        if len(lines) >= 3:
            break
        lines.append(fact)
    return lines[:3]


def _table(trend: pd.DataFrame, kpi: str, dimension: str, sort_by_value: bool = True,
           order: list[str] | None = None, unit: str = "") -> list[dict]:
    df = trend[(trend["kpi_name"] == kpi) & (trend["dimension"] == dimension)].copy()
    if order:
        df["_o"] = df["dimension_value"].map({v: i for i, v in enumerate(order)})
        df = df.sort_values("_o")
    elif sort_by_value:
        df = df.sort_values(["value", "dimension_value"], ascending=[False, True])
    return [
        {
            "name": r["dimension_value"],
            "value": fmt(r["value"], unit),
            "arrow": _arrow(_num(r["wow_change"])),
            "wow": "" if _num(r["wow_change"]) is None else f"{r['wow_change']:+,.0f}",
            "avg": fmt(r["rolling_4wk_avg"], unit),
        }
        for _, r in df.iterrows()
    ]


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of dicts with NaN/NA turned into None (templates expect None)."""
    return [{k: (None if pd.isna(v) else v) for k, v in row.items()} for row in df.to_dict("records")]


@dataclass
class ReportContext:
    week_label: str
    week_start: str
    week_end: str
    run_date: str
    generated_at: str
    pipeline_version: str
    headlines: list[Headline]
    changes: list[str]
    tables: dict
    stopped_trials: list[dict]
    transitions: list[dict]
    figures: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    weeks_of_history: int = 0
    is_backfill: bool = False


def build_context(con: duckdb.DuckDBPyConnection, ctx: RunContext, trend: pd.DataFrame,
                  figures: list[dict]) -> ReportContext:
    phase_order = [r[0] for r in con.execute("SELECT phase_group FROM dim_phase_group ORDER BY sort_order").fetchall()]
    weeks = con.execute("SELECT COUNT(DISTINCT week_start) FROM kpi_weekly").fetchone()[0]
    tables = {
        "phase": _table(trend, "new_trials", "phase", order=phase_order),
        "sponsor_class": [r for r in _table(trend, "new_trials", "sponsor_class") if r["value"] != "0"],
        "conditions": _table(trend, "new_trials", "condition_top10"),
        "countries": _table(trend, "new_trial_sites", "country_top10"),
        "stop_reasons": _table(trend, "stopped_trials", "stop_reason"),
        "enrollment": _table(trend, "median_planned_enrollment", "phase", order=phase_order),
        "to_status": [r for r in _table(trend, "status_changes", "to_status") if r["value"] != "0"],
    }
    stopped = [] if ctx.is_backfill else _records(con.execute(read_sql("report/stopped_trials.sql")).df())
    transitions = _records(con.execute(read_sql("report/status_transitions.sql")).df())

    notes = []
    status_total = trend[(trend["kpi_name"] == "status_changes") & (trend["dimension"] == "total")]
    if not status_total.empty and status_total["value"].iloc[0] == 0:
        notes.append("Confirmed status changes come from the SCD2 history: a change is only counted once a "
                     "study has been seen before with a different status. The count grows as the warehouse "
                     "accumulates weekly snapshots.")
    if ctx.is_backfill:
        notes.append("This week was loaded after the fact (backfill), so point-in-time KPIs "
                     "(registry updates, stopped trials) are not reported for it.")
    if weeks < 4:
        notes.append(f"Only {weeks} week(s) of history so far; 4-week averages use the weeks available.")
    notes.append("Planned enrollment uses trials whose enrollment type is ESTIMATED. Month-only registry "
                 "dates are normalised to the 1st of the month.")

    return ReportContext(
        week_label=ctx.label,
        week_start=ctx.week_start.isoformat(),
        week_end=ctx.week_end.isoformat(),
        run_date=ctx.run_date.isoformat(),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        pipeline_version=PIPELINE_VERSION,
        headlines=build_headlines(trend),
        changes=things_that_changed(trend),
        tables=tables,
        stopped_trials=stopped,
        transitions=transitions,
        figures=figures,
        notes=notes,
        weeks_of_history=weeks,
        is_backfill=ctx.is_backfill,
    )


def make_figures(con: duckdb.DuckDBPyConnection, ctx: RunContext, trend: pd.DataFrame,
                 figures_dir: Path) -> list[dict]:
    weekly = con.execute(
        "SELECT week_start, value, rolling_4wk_avg FROM kpi_trend "
        "WHERE kpi_name = 'new_trials' AND dimension = 'total' AND week_start <= ? ORDER BY week_start",
        [ctx.week_start],
    ).df()
    phase_order = [r[0] for r in con.execute("SELECT phase_group FROM dim_phase_group ORDER BY sort_order").fetchall()]
    phases = trend[(trend["kpi_name"] == "new_trials") & (trend["dimension"] == "phase")].copy()
    phases["_o"] = phases["dimension_value"].map({v: i for i, v in enumerate(phase_order)})
    phases = phases.sort_values("_o")
    countries = trend[(trend["kpi_name"] == "new_trial_sites") & (trend["dimension"] == "country_top10")]

    specs = [
        ("weekly_new_trials.png", "New trials registered per week with 4-week rolling average",
         lambda p: charts.weekly_trend(weekly, p)),
        ("new_trials_by_phase.png", "New trials by phase this week versus the 4-week average",
         lambda p: charts.phase_breakdown(phases, p)),
        ("top_countries_new_sites.png", "Top 10 countries by new trial sites this week",
         lambda p: charts.top_countries(countries, p)),
    ]
    figures = []
    for filename, alt, draw in specs:
        draw(figures_dir / filename)
        figures.append({"file": filename, "cid": filename.replace(".png", ""), "alt": alt})
    log(logger, logging.INFO, "figures written", figures=[f["file"] for f in figures])
    return figures


def render(report_ctx: ReportContext, templates_dir: Path, template: str, image_mode: str = "file") -> str:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "j2"]) if template.endswith(".html.j2") else False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tmpl = env.get_template(template)
    return tmpl.render(r=report_ctx, image_mode=image_mode)


@dataclass
class PublishResult:
    markdown_path: Path
    html_path: Path
    csv_path: Path
    figures: list[Path]
    email_html: str
    report_ctx: ReportContext


def publish(con: duckdb.DuckDBPyConnection, ctx: RunContext, paths: Paths) -> PublishResult:
    trend = week_trend(con, ctx)
    if trend.empty:
        raise RuntimeError(f"No KPI rows for week {ctx.week_start}; nothing to publish")
    append_kpi_history(trend, paths.kpi_history_csv, ctx)

    figures = make_figures(con, ctx, trend, paths.figures_dir)
    report_ctx = build_context(con, ctx, trend, figures)

    md = render(report_ctx, paths.templates_dir, "report.md.j2")
    html = render(report_ctx, paths.templates_dir, "report.html.j2", image_mode="file")
    email_html = render(report_ctx, paths.templates_dir, "report.html.j2", image_mode="cid")

    md_path = paths.reports_dir / "latest.md"
    html_path = paths.reports_dir / "latest.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    log(logger, logging.INFO, "report rendered", markdown=str(md_path), html=str(html_path),
        things_that_changed=report_ctx.changes)
    return PublishResult(md_path, html_path, paths.kpi_history_csv,
                         [paths.figures_dir / f["file"] for f in figures], email_html, report_ctx)
