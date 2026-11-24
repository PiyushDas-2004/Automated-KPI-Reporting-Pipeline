"""End-to-end orchestration (mocked API), state snapshot round-trip, report and email."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from pipeline import emailer, quality, report, run, state, transform
from pipeline.extract import ClinicalTrialsClient

from .conftest import WEEK1, WEEK2, FakeResponse, FakeSession, records, week1_studies, week2_studies


def fake_client(studies):
    session = FakeSession([FakeResponse(200, {"studies": studies, "totalCount": len(studies)})])
    return ClinicalTrialsClient(session=session, min_interval=0, sleep=lambda s: None)


def test_end_to_end_run_publishes_and_snapshots(tmp_paths):
    rc = run.run(date(2026, 11, 23), send_email=True, paths=tmp_paths, client=fake_client(week2_studies()))
    assert rc == 0
    for p in ("latest.md", "latest.html", "kpi_history.csv",
              "figures/weekly_new_trials.png", "figures/new_trials_by_phase.png",
              "figures/top_countries_new_sites.png"):
        assert (tmp_paths.reports_dir / p).exists(), p
    assert state.snapshot_exists(tmp_paths.state_dir)
    html = (tmp_paths.reports_dir / "latest.html").read_text()
    assert "3 things that changed this week" in html and 'src="figures/weekly_new_trials.png"' in html

    csv = pd.read_csv(tmp_paths.kpi_history_csv)
    assert set(csv["week_start"]) == {"2026-11-16"}
    assert csv.loc[(csv.kpi_name == "new_trials") & (csv.dimension == "total"), "value"].item() == 5


def test_rerun_from_snapshot_is_idempotent(tmp_paths):
    run.run(date(2026, 11, 23), paths=tmp_paths, client=fake_client(week2_studies()))
    csv_before = pd.read_csv(tmp_paths.kpi_history_csv)

    tmp_paths.warehouse.unlink()  # simulate a fresh GitHub Actions VM: only the snapshot survives
    run.run(date(2026, 11, 24), paths=tmp_paths, client=fake_client(week2_studies()))
    csv_after = pd.read_csv(tmp_paths.kpi_history_csv)

    assert len(csv_after) == len(csv_before)
    con = transform.connect(tmp_paths.warehouse)
    assert transform.table_counts(con)["studies_current"] == 8
    assert con.execute("SELECT COUNT(*) FROM study_status_history").fetchone()[0] == 8


def test_snapshot_round_trip_preserves_tables(two_week_con, tmp_path):
    before = transform.table_counts(two_week_con)
    state.save(two_week_con, tmp_path)
    fresh = transform.connect(":memory:")
    assert state.restore(fresh, tmp_path) is True
    assert transform.table_counts(fresh) == before
    assert fresh.execute("SELECT * FROM study_status_history ORDER BY ALL").fetchall() == \
        two_week_con.execute("SELECT * FROM study_status_history ORDER BY ALL").fetchall()


def test_quality_failure_stops_run_before_publish_and_snapshot(tmp_paths):
    with pytest.raises(quality.DataQualityError):
        run.run(date(2026, 11, 23), paths=tmp_paths, client=fake_client([]))
    assert not (tmp_paths.reports_dir / "latest.html").exists()
    assert not state.snapshot_exists(tmp_paths.state_dir)


def test_main_returns_non_zero_exit_code_on_failure(tmp_paths, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("API down")
    monkeypatch.setattr(run, "run", boom)
    assert run.main(["--run-date", "2026-11-23", "--no-email"]) == 1


def test_biggest_movements_ranked_by_poisson_z_score(two_week_con):
    trend = report.week_trend(two_week_con, WEEK2)
    # with the default min_base=10 only "registry updates" (4 + 8 = 12) has enough volume
    assert [line.split(" rose")[0] for line in report.biggest_movements(trend)] == ["Registry updates"]
    lines = report.biggest_movements(trend, min_base=5)
    # z(updates 4->8) = 4/sqrt(12) = 1.15 beats z(new trials 2->5) = 3/sqrt(7) = 1.13
    assert lines[0] == "Registry updates rose 100% week over week (4 → 8), above the 4-week average of 6."
    assert lines[1].startswith("New trial registrations rose 150% week over week (2 → 5)")


def test_things_that_changed_always_has_three_items(two_week_con):
    trend = report.week_trend(two_week_con, WEEK2)
    assert len(report.things_that_changed(trend)) == 3


def test_headline_arrows(two_week_con):
    heads = {h.label: h for h in report.build_headlines(report.week_trend(two_week_con, WEEK2))}
    new = heads["New trials registered"]
    assert (new.value, new.arrow) == ("5", "▲")
    assert new.delta.startswith("+3 (+150.0%)")


def test_email_skipped_when_secrets_missing(tmp_paths, caplog):
    assert emailer.send_report("s", "<p>x</p>", "x", []) is False
    assert "email skipped" in caplog.text


def test_email_sent_with_starttls_and_inline_images(monkeypatch, tmp_path):
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "app-password-123")
    monkeypatch.setenv("REPORT_TO", "a@example.com, b@example.com")
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): calls.append(("starttls",))
        def login(self, u, p): calls.append(("login", u))
        def send_message(self, msg): calls.append(("send", msg["To"], msg.as_string()))

    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    assert emailer.send_report("Weekly", '<img src="cid:chart">', "text", [("chart", png)]) is True
    assert calls[0] == ("connect", "smtp.gmail.com", 587)
    assert ("starttls",) in calls and calls.index(("starttls",)) < [c[0] for c in calls].index("login")
    sent = [c for c in calls if c[0] == "send"][0]
    assert sent[1] == "a@example.com, b@example.com"
    assert "Content-ID: <chart>" in sent[2]


def test_credentials_are_masked_in_logs(monkeypatch, capsys):
    import logging
    from pipeline.logging_utils import setup_logging
    monkeypatch.setenv("SMTP_PASS", "super-secret-pass")
    setup_logging(None)
    logging.getLogger("t").info("oops super-secret-pass leaked")
    assert "super-secret-pass" not in capsys.readouterr().out
