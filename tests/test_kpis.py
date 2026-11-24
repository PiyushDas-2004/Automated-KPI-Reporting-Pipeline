"""KPI SQL on a small fixture dataset with hand-computed expected values.

Week 2 (2026-11-16 .. 2026-11-22) contains 5 new trials:
    NCT..001  Phase 2    INDUSTRY  Obesity, Diabetes  100 est.  US, US, Canada   RECRUITING
    NCT..002  Phase 2    INDUSTRY  obesity            200 est.  US               NOT_YET_RECRUITING
    NCT..003  Phase 1    OTHER     Asthma              30 est.  France           RECRUITING
    NCT..004  Phase 1/2  NIH       Obesity             60 ACTUAL (no sites)      RECRUITING
    NCT..005  Observ.    OTHER     Asthma             500 est.  China, China     NOT_YET_RECRUITING
plus 3 older studies updated this week: 901 RECRUITING->TERMINATED ("Slow accrual"),
902 WITHDRAWN (first seen, no reason), 903 ACTIVE_NOT_RECRUITING->COMPLETED.
Week 1 had 2 new trials (one Phase 3, one Phase 2).
"""
from __future__ import annotations

from datetime import date

import pytest

from .conftest import WEEK2


def kpi(con, name, dimension="total", value="All", week=WEEK2.week_start):
    row = con.execute(
        "SELECT value FROM kpi_weekly WHERE week_start = ? AND kpi_name = ? AND dimension = ? AND dimension_value = ?",
        [week, name, dimension, value],
    ).fetchone()
    return None if row is None else row[0]


def breakdown(con, name, dimension, nonzero=True):
    rows = con.execute(
        "SELECT dimension_value, value FROM kpi_weekly WHERE week_start = ? AND kpi_name = ? AND dimension = ?",
        [WEEK2.week_start, name, dimension],
    ).fetchall()
    return {k: v for k, v in rows if v or not nonzero}


def test_new_trials_total_phase_and_sponsor_class(two_week_con):
    c = two_week_con
    assert kpi(c, "new_trials") == 5
    assert breakdown(c, "new_trials", "phase") == {
        "Phase 2": 2, "Phase 1": 1, "Phase 1/2": 1, "Observational / other": 1}
    # every phase in the reference dimension gets a row, zeros included (no gaps for trends)
    assert kpi(c, "new_trials", "phase", "Phase 3") == 0
    assert breakdown(c, "new_trials", "sponsor_class") == {"INDUSTRY": 2, "OTHER": 2, "NIH": 1}
    assert kpi(c, "industry_share_new_trials_pct") == pytest.approx(40.0)


def test_top_conditions_are_case_insensitive_and_ranked(two_week_con):
    top = two_week_con.execute(
        "SELECT dimension_value, value FROM kpi_weekly WHERE week_start = ? AND kpi_name = 'new_trials' "
        "AND dimension = 'condition_top10' ORDER BY value DESC, dimension_value", [WEEK2.week_start]).fetchall()
    assert top == [("Obesity", 3), ("Asthma", 2), ("Diabetes", 1)]


def test_status_changes_and_stop_reasons(two_week_con):
    c = two_week_con
    assert kpi(c, "status_changes") == 2  # 901 and 903; 902 is first-seen, not a change
    assert breakdown(c, "status_changes", "to_status") == {"TERMINATED": 1, "COMPLETED": 1}
    assert kpi(c, "updated_trials") == 8
    assert breakdown(c, "updated_trials", "current_status") == {
        "RECRUITING": 3, "NOT_YET_RECRUITING": 2, "TERMINATED": 1, "WITHDRAWN": 1, "COMPLETED": 1}
    assert kpi(c, "stopped_trials") == 2
    assert breakdown(c, "stopped_trials", "stop_reason") == {
        "Low enrollment / recruitment": 1, "Not provided": 1}


def test_median_planned_enrollment_uses_estimated_only(two_week_con):
    c = two_week_con
    assert kpi(c, "median_planned_enrollment") == pytest.approx(150.0)  # median(30, 100, 200, 500)
    assert kpi(c, "median_planned_enrollment", "phase", "Phase 2") == pytest.approx(150.0)
    assert kpi(c, "median_planned_enrollment", "phase", "Phase 1") == pytest.approx(30.0)
    assert kpi(c, "median_planned_enrollment", "phase", "Phase 1/2") is None  # only an ACTUAL count


def test_new_sites_by_country(two_week_con):
    c = two_week_con
    assert kpi(c, "new_trial_sites") == 7
    assert kpi(c, "countries_with_new_sites") == 4
    assert breakdown(c, "new_trial_sites", "country_top10") == {
        "United States": 3, "China": 2, "Canada": 1, "France": 1}


def test_week_over_week_and_rolling_average(two_week_con):
    row = two_week_con.execute(
        "SELECT value, prev_week_value, wow_change, wow_pct, rolling_4wk_avg, weeks_in_rolling_avg "
        "FROM kpi_trend WHERE week_start = ? AND kpi_name = 'new_trials' AND dimension = 'total'",
        [WEEK2.week_start]).fetchone()
    assert row == (5, 2, 3, 150.0, 3.5, 2)

    phase2 = two_week_con.execute(
        "SELECT prev_week_value, wow_change, wow_pct FROM kpi_trend WHERE week_start = ? "
        "AND kpi_name = 'new_trials' AND dimension = 'phase' AND dimension_value = 'Phase 2'",
        [WEEK2.week_start]).fetchone()
    assert phase2 == (1, 1, 100.0)


def test_wow_is_null_when_previous_week_is_missing(con):
    """A gap week must not be treated as 'last week'."""
    from pipeline import transform
    from pipeline.config import RunContext
    from .conftest import make_study, records

    transform.load_and_transform(con, RunContext.for_run_date(date(2026, 10, 26)),
                                 records(make_study("NCT1", first_post="2026-10-20")))
    transform.load_and_transform(con, WEEK2, records(make_study("NCT2", first_post="2026-11-17")))
    prev, avg, n = con.execute(
        "SELECT prev_week_value, rolling_4wk_avg, weeks_in_rolling_avg FROM kpi_trend "
        "WHERE week_start = ? AND kpi_name = 'new_trials' AND dimension = 'total'", [WEEK2.week_start]).fetchone()
    assert prev is None
    assert (avg, n) == (1.0, 1)  # week 2026-10-19 is outside the 4-week window (10-26 .. 11-16)


def test_backfill_weeks_skip_point_in_time_kpis(con):
    from pipeline import transform
    from pipeline.config import RunContext
    from .conftest import records, week2_studies

    transform.load_and_transform(con, RunContext.for_week(WEEK2.week_start), records(*week2_studies()))
    names = {r[0] for r in con.execute("SELECT DISTINCT kpi_name FROM kpi_weekly").fetchall()}
    assert "new_trials" in names and "status_changes" in names
    assert "updated_trials" not in names and "stopped_trials" not in names
    assert con.execute("SELECT bool_and(is_backfill) FROM kpi_weekly").fetchone()[0] is True
