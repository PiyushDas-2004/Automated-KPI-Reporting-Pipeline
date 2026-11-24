"""API pagination, retry/backoff and field flattening (all HTTP is mocked)."""
from __future__ import annotations

import json
from datetime import date

import pytest
import requests

from pipeline.config import RunContext
from pipeline.extract import (ClinicalTrialsClient, ExtractError, build_filter, extract, flatten_study,
                              load_raw_pages, normalize_date)

from .conftest import FakeResponse, FakeSession, make_study

CTX = RunContext.for_run_date(date(2026, 11, 23))


def client_for(responses, **kw):
    sleeps = []
    session = FakeSession(responses)
    client = ClinicalTrialsClient(session=session, min_interval=0, sleep=sleeps.append, **kw)
    return client, session, sleeps


def page(ids, token=None, total=None):
    body = {"studies": [make_study(i) for i in ids]}
    if token:
        body["nextPageToken"] = token
    if total is not None:
        body["totalCount"] = total
    return FakeResponse(200, body)


def test_pagination_follows_next_page_token_until_last_page(tmp_path):
    client, session, _ = client_for([
        page(["NCT1", "NCT2"], token="tok-2", total=5),
        page(["NCT3", "NCT4"], token="tok-3"),
        page(["NCT5"]),
    ])
    recs = extract(CTX, tmp_path, client)

    assert sorted(r["nct_id"] for r in recs) == ["NCT1", "NCT2", "NCT3", "NCT4", "NCT5"]
    assert len(session.calls) == 3
    assert "pageToken" not in session.calls[0]
    assert session.calls[1]["pageToken"] == "tok-2"
    assert session.calls[2]["pageToken"] == "tok-3"
    # filter and field list are sent on every page
    assert all(c["filter.advanced"] == build_filter(CTX) for c in session.calls)
    assert all("NCTId" in c["fields"] for c in session.calls)

    run_dir = tmp_path / "2026-11-23"
    assert sorted(p.name for p in run_dir.glob("page_*.json")) == ["page_0001.json", "page_0002.json", "page_0003.json"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["studies"] == 5 and manifest["pages"] == 3 and manifest["api_total_count"] == 5


def test_duplicate_ids_across_pages_are_deduplicated(tmp_path):
    client, _, _ = client_for([page(["NCT1", "NCT2"], token="t"), page(["NCT2", "NCT3"])])
    assert len(extract(CTX, tmp_path, client)) == 3


def test_raw_pages_can_be_replayed(tmp_path):
    client, _, _ = client_for([page(["NCT1"], token="t"), page(["NCT2"])])
    extract(CTX, tmp_path, client)
    assert sorted(r["nct_id"] for r in load_raw_pages(tmp_path, CTX.run_date)) == ["NCT1", "NCT2"]


def test_retries_429_with_exponential_backoff_and_retry_after():
    client, session, sleeps = client_for([
        FakeResponse(429),
        FakeResponse(503),
        FakeResponse(429, headers={"Retry-After": "10"}),
        page(["NCT1"]),
    ], backoff_base=2.0)
    pages = list(client.iter_pages("x"))
    assert len(pages) == 1 and len(session.calls) == 4
    assert sleeps == [1.0, 2.0, 10.0]  # 2**0, 2**1, then Retry-After wins over 2**2


def test_retries_network_errors():
    client, session, sleeps = client_for([requests.ConnectionError("boom"), page(["NCT1"])])
    assert len(list(client.iter_pages("x"))) == 1
    assert sleeps == [1.0]


def test_gives_up_after_max_retries():
    client, session, _ = client_for([FakeResponse(500)] * 4, max_retries=3)
    with pytest.raises(ExtractError, match="after 4 attempts"):
        list(client.iter_pages("x"))
    assert len(session.calls) == 4


def test_non_retryable_error_fails_immediately():
    client, session, sleeps = client_for([FakeResponse(400, {"error": "bad filter"})])
    with pytest.raises(ExtractError, match="HTTP 400"):
        list(client.iter_pages("x"))
    assert len(session.calls) == 1 and sleeps == []


def test_repeated_page_token_is_detected():
    client, _, _ = client_for([page(["NCT1"], token="same"), page(["NCT2"], token="same")])
    with pytest.raises(ExtractError, match="repeated pageToken"):
        list(client.iter_pages("x"))


def test_filter_covers_new_and_updated_studies_for_the_week():
    f = build_filter(CTX)
    assert f == ("AREA[StudyFirstPostDate]RANGE[2026-11-16,2026-11-22] OR "
                 "AREA[LastUpdatePostDate]RANGE[2026-11-16,2026-11-22]")


def test_flatten_keeps_only_required_fields():
    rec = flatten_study(make_study("NCT9", status="TERMINATED", why_stopped="Funding",
                                   countries=("France", "France", "Spain")))
    assert rec["nct_id"] == "NCT9"
    assert rec["overall_status"] == "TERMINATED"
    assert rec["why_stopped"] == "Funding"
    assert rec["location_countries"] == ["France", "France", "Spain"]
    assert rec["start_date"] == "2026-12-01"  # month-only date normalised
    assert set(rec) == {
        "nct_id", "brief_title", "overall_status", "phases", "study_type", "conditions",
        "lead_sponsor_name", "lead_sponsor_class", "enrollment_count", "enrollment_type", "start_date",
        "primary_completion_date", "completion_date", "first_post_date", "last_update_post_date",
        "why_stopped", "location_countries",
    }


@pytest.mark.parametrize("raw,expected", [("2026-11-05", "2026-11-05"), ("2026-11", "2026-11-01"),
                                          ("2026", "2026-01-01"), (None, None), ("", None)])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_reporting_window_is_last_complete_iso_week():
    for d in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 29)):  # Mon, Tue, Sun
        ctx = RunContext.for_run_date(d)
        assert (ctx.week_start, ctx.week_end) == (date(2026, 11, 16), date(2026, 11, 22))
