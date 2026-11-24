-- =============================================================================
-- 42_kpi_status_changes.sql - status movement this week.
--   status_changes   : transitions confirmed by the SCD2 history (previous -> new status)
--   updated_trials   : studies with a registry update this week, by current status
--   stopped_trials   : updated studies now TERMINATED / WITHDRAWN / SUSPENDED, by stop reason
-- updated_trials and stopped_trials are only computed for live (non-backfill) runs.
-- =============================================================================
INSERT INTO kpi_weekly (week_start, week_end, kpi_name, dimension, dimension_value, value, run_date, is_backfill)
WITH rc AS (
    SELECT * FROM run_context
),
confirmed_changes AS (
    SELECT h.*
    FROM study_status_history h
    CROSS JOIN rc
    WHERE h.detected_week_start = rc.week_start
      AND h.previous_status IS NOT NULL
),
updated AS (
    SELECT s.*
    FROM studies_current s
    CROSS JOIN rc
    WHERE s.last_update_post_date BETWEEN rc.week_start AND rc.week_end
),
stopped AS (
    SELECT
        u.*,
        -- Rule order matters: the first matching rule wins (documented in docs/DECISIONS.md).
        CASE
            WHEN u.why_stopped IS NULL THEN 'Not provided'
            WHEN regexp_matches(lower(u.why_stopped), 'covid|pandemic|sars-cov') THEN 'COVID-19'
            WHEN regexp_matches(lower(u.why_stopped), 'futil|efficacy|toxic|adverse|tolera|benefit|interim analy|safety')
                 AND NOT regexp_matches(lower(u.why_stopped), 'not (been )?(based on|a consequence of|due to|related to)[^.]*safety')
                 THEN 'Safety / efficacy'
            WHEN regexp_matches(lower(u.why_stopped), 'fund|business|commercial|strateg|pipeline|portfolio|sponsor|company|priorit|development|market|policy')
                 THEN 'Business / funding decision'
            WHEN regexp_matches(lower(u.why_stopped), 'enrol|recruit|accru|participa|patient|subject|eligib|inclusion')
                 THEN 'Low enrollment / recruitment'
            WHEN regexp_matches(lower(u.why_stopped), 'investigator|\bpi\b|site|staff|logist|resource|irb|availab|supply')
                 THEN 'Study team / site / logistics'
            ELSE 'Other'
        END AS reason_category
    FROM updated u
    WHERE u.overall_status IN ('TERMINATED', 'WITHDRAWN', 'SUSPENDED')
),
unioned AS (
    SELECT 'status_changes' AS kpi_name, 'total' AS dimension, 'All' AS dimension_value, COUNT(*)::DOUBLE AS value
    FROM confirmed_changes
    UNION ALL
    SELECT 'status_changes', 'to_status', d.status, COUNT(c.nct_id)
    FROM dim_status d LEFT JOIN confirmed_changes c ON c.overall_status = d.status
    GROUP BY d.status
    UNION ALL
    SELECT 'updated_trials', 'total', 'All', COUNT(*) FROM updated
    UNION ALL
    SELECT 'updated_trials', 'current_status', d.status, COUNT(u.nct_id)
    FROM dim_status d LEFT JOIN updated u ON u.overall_status = d.status
    GROUP BY d.status
    UNION ALL
    SELECT 'stopped_trials', 'total', 'All', COUNT(*) FROM stopped
    UNION ALL
    SELECT 'stopped_trials', 'stop_reason', d.reason_category, COUNT(s.nct_id)
    FROM dim_stop_reason d LEFT JOIN stopped s ON s.reason_category = d.reason_category
    GROUP BY d.reason_category
)
SELECT rc.week_start, rc.week_end, u.kpi_name, u.dimension, u.dimension_value, u.value, rc.run_date, rc.is_backfill
FROM unioned u
CROSS JOIN rc
-- updated/stopped counts are point-in-time: the API only returns each study's LATEST
-- version, so they cannot be reconstructed for past weeks. Skip them when backfilling.
WHERE u.kpi_name = 'status_changes' OR NOT rc.is_backfill;
