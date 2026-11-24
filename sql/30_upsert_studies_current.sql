-- =============================================================================
-- 30_upsert_studies_current.sql - incremental, idempotent upsert on nct_id.
-- Only overwrites a row when the incoming version is at least as recent, so
-- replaying an older extract can never roll data backwards.
-- =============================================================================
MERGE INTO studies_current AS t
USING (SELECT s.*, rc.run_date FROM stg_studies s CROSS JOIN run_context rc) AS s
ON t.nct_id = s.nct_id
WHEN MATCHED AND COALESCE(s.last_update_post_date, DATE '1900-01-01')
               >= COALESCE(t.last_update_post_date, DATE '1900-01-01') THEN UPDATE SET
    brief_title             = s.brief_title,
    overall_status          = s.overall_status,
    phases                  = s.phases,
    phase_group             = s.phase_group,
    study_type              = s.study_type,
    conditions              = s.conditions,
    lead_sponsor_name       = s.lead_sponsor_name,
    lead_sponsor_class      = s.lead_sponsor_class,
    enrollment_count        = s.enrollment_count,
    enrollment_type         = s.enrollment_type,
    start_date              = s.start_date,
    primary_completion_date = s.primary_completion_date,
    completion_date         = s.completion_date,
    first_post_date         = s.first_post_date,
    last_update_post_date   = s.last_update_post_date,
    why_stopped             = s.why_stopped,
    location_countries      = s.location_countries,
    record_hash             = s.record_hash,
    last_seen_run_date      = s.run_date
WHEN NOT MATCHED THEN INSERT (
    nct_id, brief_title, overall_status, phases, phase_group, study_type, conditions,
    lead_sponsor_name, lead_sponsor_class, enrollment_count, enrollment_type, start_date,
    primary_completion_date, completion_date, first_post_date, last_update_post_date,
    why_stopped, location_countries, record_hash, first_seen_run_date, last_seen_run_date
) VALUES (
    s.nct_id, s.brief_title, s.overall_status, s.phases, s.phase_group, s.study_type, s.conditions,
    s.lead_sponsor_name, s.lead_sponsor_class, s.enrollment_count, s.enrollment_type, s.start_date,
    s.primary_completion_date, s.completion_date, s.first_post_date, s.last_update_post_date,
    s.why_stopped, s.location_countries, s.record_hash, s.run_date, s.run_date
);
