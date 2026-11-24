-- =============================================================================
-- 20_scd2_status_history.sql - SCD Type 2 on overall_status.
-- Runs BEFORE the upsert. Idempotent: a rerun with the same data finds no
-- differences and inserts nothing.
-- =============================================================================

-- 1. Which staged studies are new to us, or carry a different status than the open history row?
CREATE OR REPLACE TEMP TABLE scd2_changes AS
WITH rc AS (SELECT * FROM run_context),
current_hist AS (
    SELECT * FROM study_status_history WHERE is_current
)
SELECT
    s.nct_id,
    s.overall_status,
    h.overall_status                                   AS previous_status,
    s.why_stopped,
    COALESCE(s.last_update_post_date, rc.run_date)     AS valid_from
FROM stg_studies s
CROSS JOIN rc
LEFT JOIN current_hist h USING (nct_id)
WHERE h.nct_id IS NULL                                            -- first time we see the study
   OR (s.overall_status IS DISTINCT FROM h.overall_status         -- status changed ...
       AND COALESCE(s.last_update_post_date, rc.run_date) >= h.valid_from);  -- ... and the data is not older

-- 2. Close the open version of every study whose status changed.
UPDATE study_status_history AS h
SET valid_to = c.valid_from,
    is_current = FALSE
FROM scd2_changes AS c
WHERE h.nct_id = c.nct_id
  AND h.is_current
  AND c.previous_status IS NOT NULL;

-- 3. Open a new current version.
INSERT INTO study_status_history
SELECT c.nct_id, c.overall_status, c.previous_status, c.why_stopped,
       c.valid_from, DATE '9999-12-31', TRUE, rc.run_date, rc.week_start
FROM scd2_changes c
CROSS JOIN run_context rc;
