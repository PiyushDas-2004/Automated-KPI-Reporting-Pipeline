"""Data quality gate.

Runs ``sql/60_quality_checks.sql``. Any check with violations > 0 raises
``DataQualityError`` and the run stops BEFORE anything is published, emailed
or snapshotted, so bad data never reaches stakeholders or the persisted state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb

from .logging_utils import log
from .transform import read_sql

logger = logging.getLogger("pipeline.quality")


class DataQualityError(RuntimeError):
    def __init__(self, failures: list["CheckResult"]):
        self.failures = failures
        names = ", ".join(f"{f.name} ({f.violations} violations)" for f in failures)
        super().__init__(f"{len(failures)} data quality check(s) failed: {names}")


@dataclass
class CheckResult:
    name: str
    description: str
    violations: int

    @property
    def passed(self) -> bool:
        return self.violations == 0


def run_checks(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    rows = con.execute(read_sql("60_quality_checks.sql")).fetchall()
    return [CheckResult(name, desc, int(v)) for name, desc, v in rows]


def enforce(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    results = run_checks(con)
    for r in results:
        log(logger, logging.INFO if r.passed else logging.ERROR, "quality check",
            check=r.name, passed=r.passed, violations=r.violations)
    failures = [r for r in results if not r.passed]
    if failures:
        raise DataQualityError(failures)
    log(logger, logging.INFO, "all quality checks passed", checks=len(results))
    return results
