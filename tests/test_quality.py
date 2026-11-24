"""The quality gate must pass on good data and FAIL on each kind of bad data."""
from __future__ import annotations

import pytest

from pipeline import quality, transform

from .conftest import WEEK2, records, week2_studies


def failed_checks(con):
    with pytest.raises(quality.DataQualityError) as exc:
        quality.enforce(con)
    return {f.name for f in exc.value.failures}


def test_all_checks_pass_on_good_data(two_week_con):
    results = quality.enforce(two_week_con)
    assert len(results) == 9 and all(r.passed for r in results)


def test_fails_on_duplicate_nct_id(two_week_con):
    c = two_week_con
    # drop the primary key so a duplicate can sneak in (simulates a broken load)
    c.execute("CREATE OR REPLACE TABLE studies_current AS SELECT * FROM studies_current")
    c.execute("INSERT INTO studies_current SELECT * FROM studies_current WHERE nct_id = 'NCT00000001'")
    assert "no_duplicate_nct_id" in failed_checks(c)


def test_fails_when_extract_is_empty(con):
    transform.load_and_transform(con, WEEK2, [])
    assert {"extract_row_count_positive", "current_row_count_positive"} <= failed_checks(con)


def test_fails_on_future_first_post_date(two_week_con):
    two_week_con.execute("UPDATE studies_current SET first_post_date = DATE '2099-01-01' WHERE nct_id = 'NCT00000003'")
    assert failed_checks(two_week_con) == {"no_future_first_post_dates"}


def test_fails_when_kpis_not_refreshed_for_this_run(two_week_con):
    two_week_con.execute("DELETE FROM kpi_weekly WHERE week_start = ?", [WEEK2.week_start])
    assert failed_checks(two_week_con) == {"kpi_refreshed_this_run"}


def test_fails_when_kpis_are_stale(two_week_con):
    two_week_con.execute("UPDATE kpi_weekly SET run_date = DATE '2026-01-01'")
    assert "kpi_refreshed_this_run" in failed_checks(two_week_con)


def test_fails_when_scd2_history_is_inconsistent(two_week_con):
    two_week_con.execute("UPDATE study_status_history SET is_current = TRUE WHERE nct_id = 'NCT00000901'")
    two_week_con.execute("UPDATE studies_current SET overall_status = 'RECRUITING' WHERE nct_id = 'NCT00000903'")
    assert {"scd2_one_current_row", "scd2_matches_current"} <= failed_checks(two_week_con)


def test_failure_message_names_the_failed_checks(con):
    transform.load_and_transform(con, WEEK2, [])
    with pytest.raises(quality.DataQualityError, match="extract_row_count_positive"):
        quality.enforce(con)


def test_good_data_rerun_still_passes(con):
    transform.load_and_transform(con, WEEK2, records(*week2_studies()))
    transform.load_and_transform(con, WEEK2, records(*week2_studies()))
    quality.enforce(con)
