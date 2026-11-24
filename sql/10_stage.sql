-- =============================================================================
-- 10_stage.sql - type, clean and enrich this run's extract.
-- Input: raw_extract (registered from Python), run_context.
-- =============================================================================
CREATE OR REPLACE TABLE stg_studies AS
WITH typed AS (
    SELECT
        trim(nct_id)                                  AS nct_id,
        brief_title,
        upper(overall_status)                         AS overall_status,
        COALESCE(phases, [])::VARCHAR[]               AS phases,
        study_type,
        COALESCE(conditions, [])::VARCHAR[]           AS conditions,
        lead_sponsor_name,
        COALESCE(lead_sponsor_class, 'UNKNOWN')       AS lead_sponsor_class,
        TRY_CAST(enrollment_count AS INTEGER)         AS enrollment_count,
        enrollment_type,
        TRY_CAST(start_date AS DATE)                  AS start_date,
        TRY_CAST(primary_completion_date AS DATE)     AS primary_completion_date,
        TRY_CAST(completion_date AS DATE)             AS completion_date,
        TRY_CAST(first_post_date AS DATE)             AS first_post_date,
        TRY_CAST(last_update_post_date AS DATE)       AS last_update_post_date,
        NULLIF(trim(why_stopped), '')                 AS why_stopped,
        COALESCE(location_countries, [])::VARCHAR[]   AS location_countries
    FROM raw_extract
    WHERE nct_id IS NOT NULL
),
dedup AS (
    -- defensive: keep the most recently updated version if an id appears twice
    SELECT *
    FROM typed
    QUALIFY ROW_NUMBER() OVER (PARTITION BY nct_id ORDER BY last_update_post_date DESC NULLS LAST) = 1
)
SELECT
    d.*,
    CASE
        WHEN d.study_type IS DISTINCT FROM 'INTERVENTIONAL'                          THEN 'Observational / other'
        WHEN list_contains(d.phases, 'PHASE1') AND list_contains(d.phases, 'PHASE2') THEN 'Phase 1/2'
        WHEN list_contains(d.phases, 'PHASE2') AND list_contains(d.phases, 'PHASE3') THEN 'Phase 2/3'
        WHEN list_contains(d.phases, 'EARLY_PHASE1')                                 THEN 'Early Phase 1'
        WHEN list_contains(d.phases, 'PHASE1')                                       THEN 'Phase 1'
        WHEN list_contains(d.phases, 'PHASE2')                                       THEN 'Phase 2'
        WHEN list_contains(d.phases, 'PHASE3')                                       THEN 'Phase 3'
        WHEN list_contains(d.phases, 'PHASE4')                                       THEN 'Phase 4'
        ELSE 'Not applicable'
    END AS phase_group,
    md5(concat_ws('|', d.overall_status, d.last_update_post_date, d.enrollment_count, d.why_stopped,
                  array_to_string(d.phases, ','), d.lead_sponsor_name)) AS record_hash
FROM dedup d;
