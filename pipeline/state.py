"""State persistence between GitHub Actions runs.

Each Actions run starts on a fresh VM, so the DuckDB file is gone. Instead of
committing the (large, binary, constantly changing) .duckdb file, we commit a
small zstd-compressed Parquet snapshot of the tables that carry state:

    studies_current       - needed for the upsert to be incremental
    study_status_history  - needed to detect status changes (SCD2)
    kpi_weekly            - needed for week-over-week / rolling averages
    pipeline_runs         - audit trail

At the start of a run the warehouse is rebuilt from the snapshot; at the end
of a SUCCESSFUL run (after the quality gate) the snapshot is rewritten.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import duckdb

from .logging_utils import log

logger = logging.getLogger("pipeline.state")

STATE_TABLES = ("studies_current", "study_status_history", "kpi_weekly", "pipeline_runs")


def snapshot_exists(state_dir: Path) -> bool:
    return all((state_dir / f"{t}.parquet").exists() for t in STATE_TABLES)


def restore(con: duckdb.DuckDBPyConnection, state_dir: Path) -> bool:
    if not snapshot_exists(state_dir):
        log(logger, logging.INFO, "no snapshot found, starting with an empty warehouse",
            state_dir=str(state_dir))
        return False
    counts = {}
    con.execute("BEGIN TRANSACTION")
    try:
        for table in STATE_TABLES:
            path = (state_dir / f"{table}.parquet").as_posix()
            con.execute(f"DELETE FROM {table}")
            con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet('{path}')")
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    log(logger, logging.INFO, "warehouse restored from snapshot", **counts)
    return True


def save(con: duckdb.DuckDBPyConnection, state_dir: Path) -> dict[str, int]:
    """Write each state table to Parquet atomically (tmp file + rename)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    sizes = {}
    order_by = {
        "studies_current": "nct_id",
        "study_status_history": "nct_id, valid_from",
        "kpi_weekly": "week_start, kpi_name, dimension, dimension_value",
        "pipeline_runs": "run_date, week_start",
    }
    for table in STATE_TABLES:
        final = state_dir / f"{table}.parquet"
        tmp = state_dir / f".{table}.parquet.tmp"
        con.execute(
            f"COPY (SELECT * FROM {table} ORDER BY {order_by[table]}) "
            f"TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
        )
        os.replace(tmp, final)
        sizes[table] = final.stat().st_size
    log(logger, logging.INFO, "state snapshot saved", **{f"{k}_bytes": v for k, v in sizes.items()})
    return sizes
