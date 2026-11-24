"""Shared fixtures: API-shaped study builders, a fake HTTP session and a
small two-week dataset whose KPI values are known in advance."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline import transform
from pipeline.config import Paths, RunContext
from pipeline.extract import flatten_study

WEEK1 = RunContext.for_run_date(date(2026, 11, 16))  # week 2026-11-09 .. 2026-11-15
WEEK2 = RunContext.for_run_date(date(2026, 11, 23))  # week 2026-11-16 .. 2026-11-22


def make_study(nct_id, status="RECRUITING", phases=("PHASE2",), study_type="INTERVENTIONAL",
               conditions=("Obesity",), sponsor="Acme Pharma", sponsor_class="INDUSTRY",
               enrollment=100, enrollment_type="ESTIMATED", first_post="2026-11-17",
               last_update=None, why_stopped=None, countries=("United States",)) -> dict:
    """Build a study exactly as the ClinicalTrials.gov v2 API returns it."""
    status_module = {
        "overallStatus": status,
        "startDateStruct": {"date": "2026-12"},
        "primaryCompletionDateStruct": {"date": "2028-06-30"},
        "completionDateStruct": {"date": "2029-01"},
        "studyFirstPostDateStruct": {"date": first_post},
        "lastUpdatePostDateStruct": {"date": last_update or first_post},
    }
    if why_stopped:
        status_module["whyStopped"] = why_stopped
    design = {"studyType": study_type, "enrollmentInfo": {"count": enrollment, "type": enrollment_type}}
    if phases:
        design["phases"] = list(phases)
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"Study {nct_id}"},
            "statusModule": status_module,
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor, "class": sponsor_class}},
            "conditionsModule": {"conditions": list(conditions)},
            "designModule": design,
            "contactsLocationsModule": {"locations": [{"country": c, "city": "X"} for c in countries]},
        }
    }


def records(*studies) -> list[dict]:
    return [flatten_study(s) for s in studies]


# --- Week 1: two new trials + two older trials that will change status in week 2 ---------------
def week1_studies() -> list[dict]:
    return [
        make_study("NCT00000101", phases=("PHASE3",), first_post="2026-11-10", conditions=("Asthma",)),
        make_study("NCT00000102", phases=("PHASE2",), first_post="2026-11-11", conditions=("Obesity",)),
        make_study("NCT00000901", status="RECRUITING", first_post="2025-01-01", last_update="2026-11-12"),
        make_study("NCT00000903", status="ACTIVE_NOT_RECRUITING", first_post="2025-01-01", last_update="2026-11-10"),
    ]


# --- Week 2: five new trials + three updated older trials --------------------------------------
def week2_studies() -> list[dict]:
    return [
        make_study("NCT00000001", phases=("PHASE2",), sponsor_class="INDUSTRY", conditions=("Obesity", "Diabetes"),
                   enrollment=100, countries=("United States", "United States", "Canada"), first_post="2026-11-17"),
        make_study("NCT00000002", status="NOT_YET_RECRUITING", phases=("PHASE2",), sponsor_class="INDUSTRY",
                   conditions=("obesity",), enrollment=200, countries=("United States",), first_post="2026-11-18"),
        make_study("NCT00000003", phases=("PHASE1",), sponsor_class="OTHER", conditions=("Asthma",),
                   enrollment=30, countries=("France",), first_post="2026-11-19"),
        make_study("NCT00000004", phases=("PHASE1", "PHASE2"), sponsor_class="NIH", conditions=("Obesity",),
                   enrollment=60, enrollment_type="ACTUAL", countries=(), first_post="2026-11-20"),
        make_study("NCT00000005", status="NOT_YET_RECRUITING", phases=None, study_type="OBSERVATIONAL",
                   sponsor_class="OTHER", conditions=("Asthma",), enrollment=500, countries=("China", "China"),
                   first_post="2026-11-21"),
        # older studies updated this week
        make_study("NCT00000901", status="TERMINATED", why_stopped="Slow accrual", first_post="2025-01-01",
                   last_update="2026-11-18"),
        make_study("NCT00000902", status="WITHDRAWN", first_post="2025-01-01", last_update="2026-11-19"),
        make_study("NCT00000903", status="COMPLETED", first_post="2025-01-01", last_update="2026-11-20"),
    ]


@pytest.fixture
def con():
    c = transform.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def two_week_con(con):
    transform.load_and_transform(con, WEEK1, records(*week1_studies()))
    transform.load_and_transform(con, WEEK2, records(*week2_studies()))
    return con


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch) -> Paths:
    monkeypatch.setenv("KPI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KPI_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("KPI_LOG_DIR", str(tmp_path / "logs"))
    for var in ("SMTP_USER", "SMTP_PASS", "REPORT_TO"):
        monkeypatch.delenv(var, raising=False)
    return Paths.from_env()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued responses in order and records every request's params."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
