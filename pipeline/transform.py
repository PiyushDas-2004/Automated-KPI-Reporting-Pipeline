"""STEP 2 - LOAD + TRANSFORM.

Loads one run's extract into DuckDB and refreshes the KPI tables. All business
logic lives in ``sql/``; this module only orchestrates the files in order and
wraps them in a single transaction so a failure leaves the warehouse untouched.

Order matters:
    10_stage                 -> type/clean the extract into stg_studies
    20_scd2_status_history   -> compare staged status with the open history row
    30_upsert_studies_current-> MERGE on nct_id (idempotent)
    40..44 KPI files         -> delete + insert this week's KPI rows
    50_kpi_trend             -> (re)create the WoW / rolling-average view
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa

from .config import RunContext
from .logging_utils import log

logger = logging.getLogger("pipeline.transform")

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

RAW_SCHEMA = pa.schema([
    ("nct_id", pa.string()),
    ("brief_title", pa.string()),
    ("overall_status", pa.string()),
    ("phases", pa.list_(pa.string())),
    ("study_type", pa.string()),
    ("conditions", pa.list_(pa.string())),
    ("lead_sponsor_name", pa.string()),
    ("lead_sponsor_class", pa.string()),
    ("enrollment_count", pa.int64()),
    ("enrollment_type", pa.string()),
    ("start_date", pa.string()),
    ("primary_completion_date", pa.string()),
    ("completion_date", pa.string()),
    ("first_post_date", pa.string()),
    ("last_update_post_date", pa.string()),
    ("why_stopped", pa.string()),
    ("location_countries", pa.list_(pa.string())),
])

KPI_SQL_FILES = [
    "40_kpi_reset.sql",
    "41_kpi_new_trials.sql",
    "42_kpi_status_changes.sql",
    "43_kpi_enrollment.sql",
    "44_kpi_sites_by_country.sql",
    "50_kpi_trend.sql",
]


@dataclass
class LoadStats:
    staged: int
    inserted: int
    updated: int
    unchanged_or_older: int
    status_changes: int
    new_in_history: int


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def run_sql_file(con: duckdb.DuckDBPyConnection, name: str) -> None:
    con.execute(read_sql(name))


def connect(db_path: Path | str) -> duckdb.DuckDBPyConnection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    run_sql_file(con, "00_schema.sql")


def set_run_context(con: duckdb.DuckDBPyConnection, ctx: RunContext) -> None:
    con.execute(
        "CREATE OR REPLACE TABLE run_context AS "
        "SELECT ?::DATE AS run_date, ?::DATE AS week_start, ?::DATE AS week_end, ?::BOOLEAN AS is_backfill",
        [ctx.run_date, ctx.week_start, ctx.week_end, ctx.is_backfill],
    )


def _records_to_arrow(records: list[dict]) -> pa.Table:
    columns = {field.name: [r.get(field.name) for r in records] for field in RAW_SCHEMA}
    return pa.table(columns, schema=RAW_SCHEMA)


def stage(con: duckdb.DuckDBPyConnection, records: list[dict]) -> int:
    raw_extract = _records_to_arrow(records)  # noqa: F841  (referenced by name from SQL)
    con.register("raw_extract", raw_extract)
    try:
        run_sql_file(con, "10_stage.sql")
    finally:
        con.unregister("raw_extract")
    return con.execute("SELECT COUNT(*) FROM stg_studies").fetchone()[0]


def _upsert_preview(con: duckdb.DuckDBPyConnection) -> tuple[int, int, int]:
    """Classify staged rows before the MERGE so we can log inserts vs updates."""
    return con.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE t.nct_id IS NULL),
            COUNT(*) FILTER (WHERE t.nct_id IS NOT NULL AND s.record_hash <> t.record_hash
                             AND COALESCE(s.last_update_post_date, DATE '1900-01-01')
                                 >= COALESCE(t.last_update_post_date, DATE '1900-01-01')),
            COUNT(*) FILTER (WHERE t.nct_id IS NOT NULL AND (s.record_hash = t.record_hash
                             OR COALESCE(s.last_update_post_date, DATE '1900-01-01')
                                 < COALESCE(t.last_update_post_date, DATE '1900-01-01')))
        FROM stg_studies s
        LEFT JOIN studies_current t USING (nct_id)
        """
    ).fetchone()


def load_and_transform(con: duckdb.DuckDBPyConnection, ctx: RunContext, records: list[dict]) -> LoadStats:
    """Stage, SCD2, upsert and KPI refresh for one run - all or nothing."""
    con.execute("BEGIN TRANSACTION")
    try:
        set_run_context(con, ctx)
        staged = stage(con, records)

        run_sql_file(con, "20_scd2_status_history.sql")
        status_changes, new_in_history = con.execute(
            "SELECT COUNT(*) FILTER (WHERE previous_status IS NOT NULL), "
            "COUNT(*) FILTER (WHERE previous_status IS NULL) FROM scd2_changes"
        ).fetchone()

        inserted, updated, unchanged = _upsert_preview(con)
        run_sql_file(con, "30_upsert_studies_current.sql")

        for name in KPI_SQL_FILES:
            run_sql_file(con, name)

        con.execute(
            "DELETE FROM pipeline_runs WHERE run_date = ? AND week_start = ?",
            [ctx.run_date, ctx.week_start],
        )
        con.execute(
            "INSERT INTO pipeline_runs VALUES (?, ?, ?, ?, ?, ?, now()::TIMESTAMP)",
            [ctx.run_date, ctx.week_start, staged, inserted, updated, status_changes],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    stats = LoadStats(staged, inserted, updated, unchanged, status_changes, new_in_history)
    log(logger, logging.INFO, "load + transform finished", **stats.__dict__)
    return stats


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("studies_current", "study_status_history", "kpi_weekly")
    }
