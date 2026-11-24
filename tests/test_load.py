"""Upsert idempotency and SCD Type 2 status-history logic."""
from __future__ import annotations

from datetime import date

from pipeline import transform
from pipeline.config import RunContext

from .conftest import WEEK1, WEEK2, make_study, records, week1_studies, week2_studies


def counts(con):
    return transform.table_counts(con)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def test_rerunning_the_same_week_is_idempotent(con):
    recs = records(*week2_studies())
    transform.load_and_transform(con, WEEK2, recs)
    first = counts(con)
    kpis_first = con.execute("SELECT * FROM kpi_weekly ORDER BY ALL").fetchall()

    stats = transform.load_and_transform(con, WEEK2, recs)
    assert counts(con) == first
    assert con.execute("SELECT * FROM kpi_weekly ORDER BY ALL").fetchall() == kpis_first
    assert (stats.inserted, stats.updated, stats.status_changes, stats.new_in_history) == (0, 0, 0, 0)
    assert first["studies_current"] == 8


def test_upsert_updates_existing_rows_without_duplicating(con):
    transform.load_and_transform(con, WEEK1, records(make_study("NCT1", enrollment=50, first_post="2026-11-10")))
    transform.load_and_transform(con, WEEK2, records(
        make_study("NCT1", enrollment=75, first_post="2026-11-10", last_update="2026-11-18")))
    rows = con.execute("SELECT nct_id, enrollment_count, first_seen_run_date, last_seen_run_date "
                       "FROM studies_current").fetchall()
    assert rows == [("NCT1", 75, date(2026, 11, 16), date(2026, 11, 23))]


def test_older_data_never_overwrites_newer_data(con):
    transform.load_and_transform(con, WEEK2, records(
        make_study("NCT1", status="COMPLETED", enrollment=75, first_post="2026-11-10", last_update="2026-11-18")))
    # replay an older extract
    transform.load_and_transform(con, WEEK1, records(
        make_study("NCT1", status="RECRUITING", enrollment=50, first_post="2026-11-10", last_update="2026-11-12")))
    assert con.execute("SELECT overall_status, enrollment_count FROM studies_current").fetchone() == ("COMPLETED", 75)
    assert con.execute("SELECT COUNT(*) FROM study_status_history").fetchone()[0] == 1


def test_duplicate_ids_inside_one_extract_keep_latest_version(con):
    transform.load_and_transform(con, WEEK2, records(
        make_study("NCT1", status="RECRUITING", last_update="2026-11-17"),
        make_study("NCT1", status="COMPLETED", last_update="2026-11-20"),
    ))
    assert con.execute("SELECT nct_id, overall_status FROM studies_current").fetchall() == [("NCT1", "COMPLETED")]


# ---------------------------------------------------------------------------
# SCD Type 2
# ---------------------------------------------------------------------------

def history(con, nct_id):
    return con.execute(
        "SELECT overall_status, previous_status, valid_from, valid_to, is_current, detected_week_start "
        "FROM study_status_history WHERE nct_id = ? ORDER BY valid_from", [nct_id]
    ).fetchall()


def test_scd2_status_change_closes_old_row_and_opens_new_one(two_week_con):
    assert history(two_week_con, "NCT00000901") == [
        ("RECRUITING", None, date(2026, 11, 12), date(2026, 11, 18), False, date(2026, 11, 9)),
        ("TERMINATED", "RECRUITING", date(2026, 11, 18), date(9999, 12, 31), True, date(2026, 11, 16)),
    ]


def test_scd2_unchanged_status_adds_no_row(con):
    transform.load_and_transform(con, WEEK1, records(make_study("NCT1", first_post="2026-11-10")))
    transform.load_and_transform(con, WEEK2, records(
        make_study("NCT1", enrollment=999, first_post="2026-11-10", last_update="2026-11-19")))  # other field changed
    assert len(history(con, "NCT1")) == 1


def test_scd2_rerun_is_idempotent(two_week_con):
    before = two_week_con.execute("SELECT * FROM study_status_history ORDER BY ALL").fetchall()
    transform.load_and_transform(two_week_con, WEEK2, records(*week2_studies()))
    assert two_week_con.execute("SELECT * FROM study_status_history ORDER BY ALL").fetchall() == before


def test_scd2_tracks_multiple_changes_including_flip_back(con):
    week3 = RunContext.for_run_date(date(2026, 11, 30))
    transform.load_and_transform(con, WEEK1, records(make_study("NCT1", status="RECRUITING", last_update="2026-11-10")))
    transform.load_and_transform(con, WEEK2, records(make_study("NCT1", status="SUSPENDED", last_update="2026-11-18")))
    transform.load_and_transform(con, week3, records(make_study("NCT1", status="RECRUITING", last_update="2026-11-25")))
    rows = history(con, "NCT1")
    assert [(r[0], r[1], r[4]) for r in rows] == [
        ("RECRUITING", None, False), ("SUSPENDED", "RECRUITING", False), ("RECRUITING", "SUSPENDED", True)]
    # intervals are contiguous: each valid_to equals the next valid_from
    assert rows[0][3] == rows[1][2] and rows[1][3] == rows[2][2]


def test_scd2_ignores_out_of_order_older_status(con):
    transform.load_and_transform(con, WEEK2, records(make_study("NCT1", status="COMPLETED", last_update="2026-11-20")))
    transform.load_and_transform(con, WEEK1, records(make_study("NCT1", status="RECRUITING", last_update="2026-11-12")))
    assert history(con, "NCT1") == [
        ("COMPLETED", None, date(2026, 11, 20), date(9999, 12, 31), True, date(2026, 11, 16))]


def test_scd2_exactly_one_current_row_per_study(two_week_con):
    assert two_week_con.execute(
        "SELECT COUNT(*) FROM (SELECT nct_id FROM study_status_history GROUP BY nct_id "
        "HAVING COUNT(*) FILTER (WHERE is_current) <> 1)").fetchone()[0] == 0
    # 4 week-1 studies + 6 new in week 2 + 2 changes (901, 903) = 12 rows
    assert two_week_con.execute("SELECT COUNT(*) FROM study_status_history").fetchone()[0] == 12
