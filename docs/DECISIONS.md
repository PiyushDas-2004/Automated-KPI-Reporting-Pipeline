# Design decisions

This document explains, in plain language, why the pipeline is built the way it is. It ends with
likely interview questions and answers.

---

## 1. Idempotency

**What it means:** running the pipeline twice for the same week gives exactly the same result as
running it once. There are no duplicate rows, no double-counted KPIs and no extra history rows.

**Why it matters:** scheduled jobs get re-run all the time. Someone clicks "Run workflow" again, a
step fails halfway and is retried, or a run is replayed to fix a bug. If a rerun can corrupt the
numbers, nobody can trust the report.

**How it's done here:**

| Layer | Mechanism |
|---|---|
| Reporting window | The window is always the **last complete ISO week (Mon–Sun) before the run date**, so a rerun on any day of the same week targets the same week. |
| `studies_current` | `MERGE ... ON nct_id`: existing studies are updated and new ones inserted. A primary key on `nct_id` makes duplicates impossible. |
| `study_status_history` | A new SCD2 row is inserted only when the status *differs* from the open row, so a rerun finds no differences and inserts nothing. |
| `kpi_weekly` | **Delete-then-insert** for the run's week, inside the same transaction. |
| `kpi_history.csv` | Rows for the run's week are removed before the new ones are appended. |
| Raw pages | `data/raw/<run_date>/` is cleared and rewritten. |
| Whole load | One DuckDB transaction: either everything for the week is applied, or nothing is. |

The tests prove it: `test_rerunning_the_same_week_is_idempotent` loads the same data twice and
asserts identical row counts and identical KPI rows, and `test_rerun_from_snapshot_is_idempotent`
does the same across a simulated fresh Actions VM.

## 2. Incremental loading

**What it means:** each run processes only what changed (studies first posted or updated during the
week, usually 3,000–5,500), not the whole registry (500,000+ studies).

**How:** the API filter is
`AREA[StudyFirstPostDate]RANGE[week] OR AREA[LastUpdatePostDate]RANGE[week]`, and the result is
merged into the existing `studies_current` table.

**Two safety rules:**

- **Never roll data backwards.** The `MERGE` only overwrites a row when the incoming
  `last_update_post_date` is at least as recent as the stored one. Replaying an old extract can't
  undo newer data (`test_older_data_never_overwrites_newer_data`).
- **De-duplicate inside the extract.** The same study can appear on two pages, or match both filter
  conditions. Staging keeps one row per `nct_id`, the latest version, using `ROW_NUMBER()`.

**Trade-off:** incremental loads are fast and kind to the API, but they need *state* (what we
already know). That is why state persistence (section 5) exists.

## 3. SCD Type 2 (status history)

**What it means:** instead of overwriting a trial's status, every status period is kept as its own
row:

| nct_id | overall_status | previous_status | valid_from | valid_to | is_current |
|---|---|---|---|---|---|
| NCT…901 | RECRUITING | *(null)* | 2026-11-12 | 2026-11-18 | false |
| NCT…901 | TERMINATED | RECRUITING | 2026-11-18 | 9999-12-31 | true |

**Why:** "which trials moved to TERMINATED this week?" and "how long do trials stay RECRUITING?"
can't be answered from a table that only holds the current value. CROs care about these
transitions because they drive enrollment forecasts, site activity and the competitive landscape.

**How (`sql/20_scd2_status_history.sql`), run before the upsert:**

1. Compare each staged study with its **open** history row (`is_current = TRUE`).
2. If the status differs, and the incoming data isn't older than the open row, **close** the open
   row (`valid_to = new valid_from`, `is_current = FALSE`) and **insert** a new open row.
3. A study seen for the first time gets one open row with `previous_status = NULL`. That is the
   first observation, not a change, so it isn't counted as a status change.

**Choices worth defending:**

- `valid_from` is the registry's **last update post date**, a business date, not the time the
  pipeline ran. This keeps history the same no matter when the job runs.
- `valid_to` is **exclusive** and open rows use `9999-12-31`, so "status on date D" is simply
  `valid_from <= D AND D < valid_to`, with no NULL handling.
- `detected_week_start` records which run found the change. The weekly KPI counts by this column.
- Quality checks enforce the SCD2 rules: exactly one current row per study, `valid_from <= valid_to`,
  and the current history status matching `studies_current`.

**Cold start:** the public API has no version history, so SCD2 can only detect changes that happen
*after* the pipeline first sees a study. "Confirmed status changes" therefore starts at 0 and grows
week by week. The report says so. Meanwhile, "updated trials by current status" and "stopped
trials" give an immediate, clearly labelled proxy.

## 4. Why the quality checks fail the run

`sql/60_quality_checks.sql` returns one row per check. Any check with violations above 0 raises
`DataQualityError`, and the process exits with code 1.

| Check | Catches |
|---|---|
| `no_duplicate_nct_id` | a broken upsert or key, which would double-count every KPI |
| `extract_row_count_positive` | an API outage or filter bug that returned nothing (a "0 new trials" report would be wrong, not just quiet) |
| `current_row_count_positive` | a failed snapshot restore |
| `no_future_first_post_dates` | date parsing bugs or bad source data |
| `no_null_keys` | rows that can't be joined or tracked |
| `kpi_refreshed_this_run` | a KPI step that silently did nothing, leaving last week's numbers in the report |
| `scd2_one_current_row`, `scd2_valid_interval`, `scd2_matches_current` | a corrupted status history |

**Why fail instead of warn:** the main risk in automated reporting is not a missing report. It is a
**wrong report that looks right**. A failed run is visible (red job, `::error::` annotation, log
excerpt in the job summary, no email), and the fix is a rerun. A wrong email to stakeholders
damages trust and is hard to take back. The gate also runs **before** the state snapshot is
saved, so bad data is never persisted and can't spread into next week's run.

## 5. State persistence between GitHub Actions runs

**The problem:** every Actions run starts on a fresh VM, so `data/warehouse.duckdb` from last week
no longer exists. Without state, the upsert can't be incremental, SCD2 can't detect changes and
week-over-week can't be computed.

**The choice:** at the end of every *successful* run, export the stateful tables
(`studies_current`, `study_status_history`, `kpi_weekly`, `pipeline_runs`) to
**zstd-compressed Parquet** in `data/state/` and commit them with the reports. At the start of a
run, if there is no local warehouse, rebuild it from those files (`INSERT ... BY NAME SELECT *
FROM read_parquet(...)`). Writes are atomic (temporary file, then rename).

**Alternatives considered:**

| Option | Why not (for this project) |
|---|---|
| Commit the `.duckdb` file | Binary, version-specific storage format, larger, and every change rewrites the whole file. Parquet is open, columnar and readable by Power BI, pandas and Spark. |
| `actions/cache` | Caches are evicted after 7 days without access and can be dropped at any time. A weekly job sits right on that limit, so it's too fragile for the system of record. |
| Workflow artifacts | Expire (90 days maximum), and downloading the previous run's artifact needs extra API plumbing. |
| Cloud storage (S3, GCS, Azure Blob) or MotherDuck | The right answer in production, but it needs cloud credentials and costs money. Out of scope for a public portfolio project. |
| Re-pull full history every run | 500k+ studies every week is slow and unkind to a free public API, and it still can't reconstruct status history. |

**Trade-off and growth:** the snapshot is about 2.9 MB for 8 weeks and grows by about 0.3–0.5 MB
per week, because each commit stores a new version. That's fine for a few years. Beyond that, the
next steps would be: (a) move the snapshot to object storage and keep only a pointer in git;
(b) partition `studies_current` by first-post year so only changed partitions are rewritten;
(c) prune columns the KPIs don't use.

**Bonus:** because the state lives in git, every week's warehouse state can be audited and
restored with `git checkout <commit> -- data/state/`.

## 6. Scheduling choices

- **Monday 03:30 UTC (09:00 IST).** The previous ISO week (Mon–Sun) is fully closed and
  ClinicalTrials.gov has published Sunday's updates. The report is ready at the start of the
  India business day, which is Sunday night in the US, before the US work week starts.
- **Report the last complete week, not "the last 7 days".** A rolling 7-day window would give
  different numbers depending on when the job runs. Fixed ISO weeks make runs idempotent and line
  up with how business reporting works.
- **Not on the hour:** GitHub's cron is "best effort", and the top of the hour is the busiest time,
  when runs are most often delayed. :30 avoids the worst of it. Being a few minutes late doesn't
  matter, because the window is fixed.
- **`workflow_dispatch`** with an optional `run_date` input, for manual reruns and backfills.
- **`concurrency: weekly-kpi-refresh`** with `cancel-in-progress: false`: two runs can never write
  the snapshot at the same time, and a manual run queues behind a scheduled one instead of
  killing it.
- **Late runs count as backfills.** If a run happens more than 6 days after its run date,
  point-in-time KPIs are skipped, because they can't be measured after the fact.
- **Tests run before the pipeline in CI**, so a code regression fails the job before it can touch
  data.
- GitHub disables scheduled workflows in public repos after 60 days without repository activity.
  The weekly bot commit counts as activity, so the schedule keeps itself alive.

## 7. Other design choices

- **Business logic in SQL files, orchestration in Python.** Analysts can read and review the KPI
  logic without reading Python, and the same SQL runs in the DuckDB CLI.
- **Long ("tidy") KPI format.** Adding a KPI or dimension adds rows, not columns. Power BI, Tableau
  and pandas all prefer this shape, and one generic trend view covers every series.
- **Reference dimensions (`dim_phase_group`, `dim_sponsor_class`, `dim_status`, `dim_stop_reason`)**
  are left-joined, so a category with no trials this week is an explicit **0**, not a missing row.
  Missing rows make week-over-week and rolling averages wrong.
- **Week-over-week only compares adjacent weeks.** `prev_week_value` is filled only when the
  previous row is exactly 7 days earlier, so a gap never pretends to be "last week". The rolling
  average uses a `RANGE` frame over dates (the current week plus the 3 before it), not
  `ROWS 3 PRECEDING`, so missing weeks aren't silently skipped.
- **"3 things that changed" uses a Poisson z-score**, `(this − last) / √(this + last)`. For count
  data, this approximates how many standard deviations the change is. It ranks 400 → 480
  (z ≈ 2.7) above 2 → 6 (z ≈ 1.4, even though that is +200%). Breakdown rows are **mix-adjusted**:
  the expected value is last week × the total's growth, so segments that just followed the overall
  trend don't fill the list. Series with fewer than 10 trials across the two weeks are ignored.
- **Planned enrollment = `enrollment_type = 'ESTIMATED'`.** Mixing in ACTUAL counts from
  retrospectively registered trials would distort "planned".
- **Month-only dates** (`2026-11`) are normalised to the 1st of the month. This is documented in
  the report notes.
- **Structured JSON logs**: one JSON object per line with step names and durations. They are easy
  to grep in Actions, and the log file is uploaded as an artifact. SMTP secrets are never logged,
  and the log formatter masks them as a second line of defence.
- **Polite API client:** at most about 46 requests per minute (1.3 s spacing), exponential backoff
  (1, 2, 4, 8… s) on 429/5xx/network errors, `Retry-After` respected, protection against a
  repeated `pageToken`, and the maximum page size (1,000).

---

## Likely interview questions (with answers)

**1. What happens if the workflow runs twice in the same week?**
The second run targets the same ISO week, so it pulls the same window. The `MERGE` updates
existing rows instead of inserting, SCD2 finds no status differences, the week's KPI rows are
deleted and re-inserted, and the CSV replaces that week's rows. Row counts are identical, and a
test asserts exactly that. The only visible difference is a newer `run_date` stamp, and the
numbers if the registry really changed in between, which is correct behaviour.

**2. Why SCD Type 2 instead of just keeping the current status?**
The business question is about *change*: which trials started recruiting, completed or were
terminated this week. Type 1 (overwrite) loses the old value, so you can't answer that or
calculate time-in-status. Type 2 keeps every period with `valid_from` and `valid_to`, so you can
reconstruct the status on any date. I only track `overall_status` with Type 2, because that's
where the history has value. Other attributes are Type 1 in `studies_current`.

**3. How do you know the KPI numbers are right?**
In three ways. First, unit tests run the actual SQL on a small fixture where I calculated every
expected value by hand: counts by phase, case-insensitive conditions, median of ESTIMATED
enrollment only, site counts and week-over-week. Second, quality checks run on every production
run and fail it on duplicates, empty extracts, future dates, stale KPIs or broken SCD2 rules.
Third, the extract compares its study count with the API's `totalCount` and logs a warning if
they differ.

**4. Why fail the whole pipeline on a data-quality issue instead of sending the report with a warning?**
Because a wrong number in an executive email costs more than a late one. A failure is loud (red
job, annotation, no email), cheap to fix (rerun), and is caught before the state snapshot is
saved, so bad data doesn't carry into next week. Warnings tend to get ignored.

**5. DuckDB state disappears between GitHub Actions runs. How did you handle it?**
At the end of each successful run I export the stateful tables to compressed Parquet and commit
them. At the start of the next run I restore from them. I chose Parquet over committing the
.duckdb file because it's an open, smaller, columnar format, and over `actions/cache` because
caches are evicted after 7 days, which is too close to a weekly schedule. In production I'd put
the same files in S3 or ADLS, or use MotherDuck. The restore and save interface wouldn't change.

**6. How does the incremental load avoid missing or double-counting studies?**
The filter covers studies first posted OR updated in the window, so new trials and changed trials
are both captured, and the union is de-duplicated by `nct_id`. Weeks are fixed and contiguous, so
no gaps or overlaps appear between runs. The `MERGE` only applies data that is at least as recent
as what is stored, so out-of-order replays can't overwrite newer data.

**7. Walk me through how week-over-week and the 4-week rolling average are calculated.**
`kpi_trend` is a view over `kpi_weekly`. For each series (KPI × dimension × value) I use
`LAG(value)` ordered by week, but only accept it if the previous row is exactly 7 days earlier,
so a missing week returns NULL instead of a misleading comparison. The rolling average is
`AVG(value) OVER (... ORDER BY week_start RANGE BETWEEN INTERVAL 21 DAYS PRECEDING AND CURRENT ROW)`,
which covers the current week and the 3 before it by date. I also output how many weeks were
actually in the window.

**8. The API only returns the current version of each study. How does that affect your backfill?**
New registrations backfill exactly, because a first-post date never changes. Anything
point-in-time, like "updated this week" or "stopped this week", can't be reconstructed, because a
study updated again later has moved out of the older week. Instead of reporting biased numbers, I
flag backfilled weeks with `is_backfill` and skip those KPIs. They build up from live weekly runs.
Late runs (more than 6 days after the run date) are treated the same way.

**9. How did you decide which "3 things" go in the summary?**
I rank changes by a Poisson z-score, change divided by the square root of the combined count,
which is roughly how unusual the move is for count data. That stops tiny bases from dominating
with big percentages. For breakdowns I adjust for the overall trend: if the total fell 15%, a
segment that fell 15% isn't news, but one that rose 50% is. If there's no prior week, it falls
back to composition facts, such as the leading phase, the top country and the main stop reason.

**10. How would you scale or productionise this?**
Move the snapshot to object storage or a managed warehouse (Snowflake, BigQuery or MotherDuck);
move orchestration to Airflow or Dagster if there are dependencies on other pipelines; add
alerting (Slack or Teams) on failure; publish the KPI table straight to a Power BI dataset via the
REST API instead of a CSV; partition the current-state table; and add data-contract tests on the
API schema so a renamed field fails fast. The core design — idempotent weekly windows, MERGE,
SCD2 and a blocking quality gate — wouldn't change.

**Bonus: How do you keep credentials safe?**
They're only read from environment variables, which are GitHub encrypted secrets in CI. They're
never in code, config files or logs: the logger only records *which* variables are missing, never
their values, and the log formatter also masks them. `.env` is git-ignored, the staged files were scanned
for secrets before the first push, and Gmail uses an app password over STARTTLS, so the real account
password is never used.
