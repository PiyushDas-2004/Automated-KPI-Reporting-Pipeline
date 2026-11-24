"""STEP 1 - EXTRACT.

Pulls every study that was either *first posted* or *last updated* during the
reporting week from the public ClinicalTrials.gov API v2, following
``nextPageToken`` until the last page.

* Rate limits: requests are spaced out (the API allows roughly 50 requests per
  minute per IP) and 429 / 5xx / network errors are retried with exponential
  backoff, honouring ``Retry-After`` when the server sends it.
* Raw pages are written untouched to ``data/raw/<run_date>/`` so any run can be
  replayed or audited.
* Only the fields the KPIs need are requested and kept.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable, Iterator

import requests

from .config import API_FIELDS, API_URL, PAGE_SIZE, RunContext
from .logging_utils import log

logger = logging.getLogger("pipeline.extract")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExtractError(RuntimeError):
    pass


def build_filter(ctx: RunContext) -> str:
    """Essie expression: first posted OR last updated inside the reporting week."""
    rng = f"RANGE[{ctx.week_start.isoformat()},{ctx.week_end.isoformat()}]"
    return f"AREA[StudyFirstPostDate]{rng} OR AREA[LastUpdatePostDate]{rng}"


class ClinicalTrialsClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        min_interval: float = 1.3,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self.timeout = timeout
        self._sleep = sleep
        self._last_request_at = 0.0
        self.requests_made = 0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = time.monotonic()

    def get_page(self, params: dict) -> dict:
        """GET one page, retrying transient failures with exponential backoff."""
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self.session.get(API_URL, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                retry_after = None
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ExtractError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")

            if attempt == self.max_retries:
                break
            delay = self.backoff_base ** attempt
            if retry_after and str(retry_after).isdigit():
                delay = max(delay, float(retry_after))
            log(logger, logging.WARNING, "transient API error, backing off",
                attempt=attempt + 1, delay_s=delay, error=last_error)
            self._sleep(delay)
        raise ExtractError(f"API request failed after {self.max_retries + 1} attempts: {last_error}")

    def iter_pages(self, filter_expr: str, page_size: int = PAGE_SIZE) -> Iterator[dict]:
        """Yield every page of results, following nextPageToken."""
        params = {
            "filter.advanced": filter_expr,
            "fields": ",".join(API_FIELDS),
            "pageSize": page_size,
            "countTotal": "true",
            "format": "json",
        }
        seen_tokens: set[str] = set()
        while True:
            page = self.get_page(params)
            yield page
            token = page.get("nextPageToken")
            if not token:
                return
            if token in seen_tokens:  # defensive: never loop forever on a buggy token
                raise ExtractError(f"API returned a repeated pageToken: {token}")
            seen_tokens.add(token)
            params = {**params, "pageToken": token}
            params.pop("countTotal", None)


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def normalize_date(value: str | None) -> str | None:
    """ClinicalTrials.gov dates are 'YYYY-MM-DD' or 'YYYY-MM'; month-only -> 1st of month."""
    if not value:
        return None
    parts = value.split("-")
    if len(parts) == 2:
        return f"{parts[0]}-{parts[1]}-01"
    if len(parts) == 1:
        return f"{parts[0]}-01-01"
    return value


def flatten_study(study: dict) -> dict:
    ps = study.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    enrollment = design.get("enrollmentInfo", {})
    locations = ps.get("contactsLocationsModule", {}).get("locations", []) or []

    def struct_date(key: str) -> str | None:
        return normalize_date((status.get(key) or {}).get("date"))

    return {
        "nct_id": ident.get("nctId"),
        "brief_title": ident.get("briefTitle"),
        "overall_status": status.get("overallStatus"),
        "phases": design.get("phases") or [],
        "study_type": design.get("studyType"),
        "conditions": ps.get("conditionsModule", {}).get("conditions") or [],
        "lead_sponsor_name": sponsor.get("name"),
        "lead_sponsor_class": sponsor.get("class"),
        "enrollment_count": enrollment.get("count"),
        "enrollment_type": enrollment.get("type"),
        "start_date": struct_date("startDateStruct"),
        "primary_completion_date": struct_date("primaryCompletionDateStruct"),
        "completion_date": struct_date("completionDateStruct"),
        "first_post_date": struct_date("studyFirstPostDateStruct"),
        "last_update_post_date": struct_date("lastUpdatePostDateStruct"),
        "why_stopped": status.get("whyStopped"),
        # one entry per site, so len() == number of sites in that country list
        "location_countries": [loc["country"] for loc in locations if loc.get("country")],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def extract(ctx: RunContext, raw_root: Path, client: ClinicalTrialsClient | None = None) -> list[dict]:
    client = client or ClinicalTrialsClient()
    run_dir = raw_root / ctx.run_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    for old in run_dir.glob("page_*.json"):  # rerun of the same day replaces its raw pages
        old.unlink()

    filter_expr = build_filter(ctx)
    log(logger, logging.INFO, "extract started", week_start=ctx.week_start, week_end=ctx.week_end,
        filter=filter_expr)

    records: dict[str, dict] = {}
    total_count = None
    n_pages = 0
    for n_pages, page in enumerate(client.iter_pages(filter_expr), start=1):
        if total_count is None:
            total_count = page.get("totalCount")
        (run_dir / f"page_{n_pages:04d}.json").write_text(json.dumps(page), encoding="utf-8")
        for study in page.get("studies", []):
            rec = flatten_study(study)
            if rec["nct_id"]:
                records[rec["nct_id"]] = rec  # de-duplicate across pages
        log(logger, logging.INFO, "page fetched", page=n_pages, studies_so_far=len(records))

    manifest = {
        "run_date": ctx.run_date.isoformat(),
        "week_start": ctx.week_start.isoformat(),
        "week_end": ctx.week_end.isoformat(),
        "filter": filter_expr,
        "pages": n_pages,
        "api_total_count": total_count,
        "studies": len(records),
        "requests_made": client.requests_made,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if total_count is not None and total_count != len(records):
        log(logger, logging.WARNING, "study count differs from API totalCount",
            api_total_count=total_count, studies=len(records))
    log(logger, logging.INFO, "extract finished", studies_pulled=len(records), pages=n_pages)
    return list(records.values())


def load_raw_pages(raw_root: Path, run_date: date) -> list[dict]:
    """Rebuild records from saved raw pages (replay without hitting the API)."""
    run_dir = raw_root / run_date.isoformat()
    records: dict[str, dict] = {}
    for page_file in sorted(run_dir.glob("page_*.json")):
        for study in json.loads(page_file.read_text(encoding="utf-8")).get("studies", []):
            rec = flatten_study(study)
            if rec["nct_id"]:
                records[rec["nct_id"]] = rec
    return list(records.values())
