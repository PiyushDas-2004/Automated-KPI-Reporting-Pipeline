-- =============================================================================
-- 60_quality_checks.sql - every row is one check; violations > 0 FAILS the run.
-- =============================================================================
WITH rc AS (SELECT * FROM run_context)
SELECT 'no_duplicate_nct_id' AS check_name,
       'studies_current has exactly one row per nct_id' AS description,
       (SELECT COUNT(*) FROM (SELECT nct_id FROM studies_current GROUP BY nct_id HAVING COUNT(*) > 1)) AS violations
UNION ALL
SELECT 'extract_row_count_positive',
       'this run staged more than 0 studies',
       CASE WHEN (SELECT COUNT(*) FROM stg_studies) > 0 THEN 0 ELSE 1 END
UNION ALL
SELECT 'current_row_count_positive',
       'studies_current is not empty',
       CASE WHEN (SELECT COUNT(*) FROM studies_current) > 0 THEN 0 ELSE 1 END
UNION ALL
SELECT 'no_future_first_post_dates',
       'no study has a first_post_date in the future (after the run date and after today)',
       (SELECT COUNT(*) FROM studies_current, rc WHERE first_post_date > greatest(rc.run_date, current_date))
UNION ALL
SELECT 'no_null_keys',
       'nct_id and overall_status are always populated',
       (SELECT COUNT(*) FROM studies_current WHERE nct_id IS NULL OR overall_status IS NULL)
UNION ALL
SELECT 'kpi_refreshed_this_run',
       'kpi_weekly has rows for this week stamped with this run date',
       CASE WHEN (SELECT COUNT(*) FROM kpi_weekly, rc
                  WHERE kpi_weekly.week_start = rc.week_start AND kpi_weekly.run_date = rc.run_date) > 0
            THEN 0 ELSE 1 END
UNION ALL
SELECT 'scd2_one_current_row',
       'every study in the history has exactly one is_current row',
       (SELECT COUNT(*) FROM (SELECT nct_id FROM study_status_history GROUP BY nct_id
                              HAVING COUNT(*) FILTER (WHERE is_current) <> 1))
UNION ALL
SELECT 'scd2_valid_interval',
       'valid_from <= valid_to on every history row',
       (SELECT COUNT(*) FROM study_status_history WHERE valid_from > valid_to)
UNION ALL
SELECT 'scd2_matches_current',
       'current status in the history equals studies_current.overall_status',
       (SELECT COUNT(*) FROM studies_current s
        LEFT JOIN study_status_history h ON h.nct_id = s.nct_id AND h.is_current
        WHERE h.overall_status IS DISTINCT FROM s.overall_status);
