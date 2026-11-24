-- =============================================================================
-- 00_schema.sql - warehouse tables (idempotent: safe to run on every start)
-- =============================================================================

-- Current state: exactly one row per study (upserted on nct_id).
CREATE TABLE IF NOT EXISTS studies_current (
    nct_id                  VARCHAR PRIMARY KEY,
    brief_title             VARCHAR,
    overall_status          VARCHAR NOT NULL,
    phases                  VARCHAR[],
    phase_group             VARCHAR,
    study_type              VARCHAR,
    conditions              VARCHAR[],
    lead_sponsor_name       VARCHAR,
    lead_sponsor_class      VARCHAR,
    enrollment_count        INTEGER,
    enrollment_type         VARCHAR,
    start_date              DATE,
    primary_completion_date DATE,
    completion_date         DATE,
    first_post_date         DATE,
    last_update_post_date   DATE,
    why_stopped             VARCHAR,
    location_countries      VARCHAR[],   -- one element per site
    record_hash             VARCHAR,
    first_seen_run_date     DATE,
    last_seen_run_date      DATE
);

-- SCD Type 2 history of overall_status. valid_to is exclusive; open rows use 9999-12-31.
CREATE TABLE IF NOT EXISTS study_status_history (
    nct_id              VARCHAR NOT NULL,
    overall_status      VARCHAR NOT NULL,
    previous_status     VARCHAR,           -- NULL = first time we saw the study (not a change)
    why_stopped         VARCHAR,
    valid_from          DATE NOT NULL,     -- registry post date of the version that carried this status
    valid_to            DATE NOT NULL,
    is_current          BOOLEAN NOT NULL,
    detected_run_date   DATE NOT NULL,
    detected_week_start DATE NOT NULL
);

-- Long/"tidy" KPI table: one row per week x KPI x dimension value.
CREATE TABLE IF NOT EXISTS kpi_weekly (
    week_start      DATE NOT NULL,
    week_end        DATE NOT NULL,
    kpi_name        VARCHAR NOT NULL,
    dimension       VARCHAR NOT NULL,
    dimension_value VARCHAR NOT NULL,
    value           DOUBLE,
    run_date        DATE NOT NULL,
    is_backfill     BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE = week loaded after the fact
    PRIMARY KEY (week_start, kpi_name, dimension, dimension_value)
);

-- Audit trail of pipeline runs.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_date          DATE NOT NULL,
    week_start        DATE NOT NULL,
    studies_extracted INTEGER,
    rows_inserted     INTEGER,
    rows_updated      INTEGER,
    status_changes    INTEGER,
    finished_at       TIMESTAMP
);

-- Small reference dimensions so fixed categories get explicit zeros (no gaps in trends).
CREATE OR REPLACE TABLE dim_phase_group AS
SELECT * FROM (VALUES
    (1, 'Early Phase 1'), (2, 'Phase 1'), (3, 'Phase 1/2'), (4, 'Phase 2'),
    (5, 'Phase 2/3'), (6, 'Phase 3'), (7, 'Phase 4'), (8, 'Not applicable'), (9, 'Observational / other')
) AS t(sort_order, phase_group);

CREATE OR REPLACE TABLE dim_sponsor_class AS
SELECT * FROM (VALUES
    ('INDUSTRY'), ('NIH'), ('FED'), ('OTHER_GOV'), ('NETWORK'), ('INDIV'), ('OTHER'), ('AMBIG'), ('UNKNOWN')
) AS t(sponsor_class);

CREATE OR REPLACE TABLE dim_status AS
SELECT * FROM (VALUES
    ('NOT_YET_RECRUITING'), ('RECRUITING'), ('ENROLLING_BY_INVITATION'), ('ACTIVE_NOT_RECRUITING'),
    ('COMPLETED'), ('SUSPENDED'), ('TERMINATED'), ('WITHDRAWN'), ('UNKNOWN'),
    ('AVAILABLE'), ('NO_LONGER_AVAILABLE'), ('TEMPORARILY_NOT_AVAILABLE'), ('APPROVED_FOR_MARKETING'), ('WITHHELD')
) AS t(status);

CREATE OR REPLACE TABLE dim_stop_reason AS
SELECT * FROM (VALUES
    ('Low enrollment / recruitment'), ('Business / funding decision'), ('Safety / efficacy'),
    ('Study team / site / logistics'), ('COVID-19'), ('Other'), ('Not provided')
) AS t(reason_category);
