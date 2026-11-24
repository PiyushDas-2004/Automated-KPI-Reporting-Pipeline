"""Central configuration: paths, the reporting window, and run metadata.

Every path can be overridden with an environment variable so tests (and
experiments) can point the pipeline at a temporary directory without touching
the real warehouse or the committed reports.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_VERSION = "1.0.0"

API_URL = "https://clinicaltrials.gov/api/v2/studies"
PAGE_SIZE = 1000  # API maximum

# Only the fields the KPIs need (ClinicalTrials.gov "piece" names).
API_FIELDS = [
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "Phase",
    "StudyType",
    "Condition",
    "LeadSponsorName",
    "LeadSponsorClass",
    "EnrollmentCount",
    "EnrollmentType",
    "StartDate",
    "PrimaryCompletionDate",
    "CompletionDate",
    "StudyFirstPostDate",
    "LastUpdatePostDate",
    "WhyStopped",
    "LocationCountry",
]


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    raw_dir: Path
    state_dir: Path
    warehouse: Path
    reports_dir: Path
    figures_dir: Path
    kpi_history_csv: Path
    log_dir: Path
    sql_dir: Path
    templates_dir: Path

    @classmethod
    def from_env(cls) -> "Paths":
        data_dir = Path(os.environ.get("KPI_DATA_DIR", PROJECT_ROOT / "data"))
        reports_dir = Path(os.environ.get("KPI_REPORTS_DIR", PROJECT_ROOT / "reports"))
        return cls(
            data_dir=data_dir,
            raw_dir=data_dir / "raw",
            state_dir=data_dir / "state",
            warehouse=data_dir / "warehouse.duckdb",
            reports_dir=reports_dir,
            figures_dir=reports_dir / "figures",
            kpi_history_csv=reports_dir / "kpi_history.csv",
            log_dir=Path(os.environ.get("KPI_LOG_DIR", PROJECT_ROOT / "logs")),
            sql_dir=PROJECT_ROOT / "sql",
            templates_dir=PROJECT_ROOT / "templates",
        )


@dataclass(frozen=True)
class RunContext:
    """The reporting window for one run.

    The window is always the most recent *complete* ISO week (Monday-Sunday)
    before ``run_date``. A scheduled Monday run therefore reports the week that
    ended the day before, and a manual rerun on any later day of the same week
    targets exactly the same window, which keeps reruns idempotent.
    """

    run_date: date
    week_start: date
    week_end: date
    # True when the week is loaded after the fact (backfill or a late rerun). Point-in-time
    # KPIs (registry updates, stopped trials) are then skipped because the API only
    # returns each study's latest version.
    is_backfill: bool = False

    @classmethod
    def for_run_date(cls, run_date: date, today: date | None = None) -> "RunContext":
        this_monday = run_date - timedelta(days=run_date.weekday())
        week_start = this_monday - timedelta(days=7)
        # A run executed more than a week after its run date is effectively a backfill.
        late = today is not None and (today - run_date).days > 6
        return cls(run_date=run_date, week_start=week_start, week_end=week_start + timedelta(days=6),
                   is_backfill=late)

    @classmethod
    def for_week(cls, week_start: date) -> "RunContext":
        """Context for a historical week, as if run on the following Monday (backfill)."""
        if week_start.weekday() != 0:
            raise ValueError(f"week_start must be a Monday, got {week_start}")
        return cls(run_date=week_start + timedelta(days=7), week_start=week_start,
                   week_end=week_start + timedelta(days=6), is_backfill=True)

    @property
    def label(self) -> str:
        return f"{self.week_start:%d %b %Y} - {self.week_end:%d %b %Y}"
