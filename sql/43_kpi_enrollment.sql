-- =============================================================================
-- 43_kpi_enrollment.sql - median PLANNED enrollment (enrollment_type = ESTIMATED)
-- of trials registered this week, overall and by phase.
-- =============================================================================
INSERT INTO kpi_weekly (week_start, week_end, kpi_name, dimension, dimension_value, value, run_date, is_backfill)
WITH rc AS (
    SELECT * FROM run_context
),
planned AS (
    SELECT s.phase_group, s.enrollment_count
    FROM studies_current s
    CROSS JOIN rc
    WHERE s.first_post_date BETWEEN rc.week_start AND rc.week_end
      AND s.enrollment_type = 'ESTIMATED'
      AND s.enrollment_count IS NOT NULL
),
unioned AS (
    SELECT 'total' AS dimension, 'All' AS dimension_value, median(enrollment_count) AS value
    FROM planned
    HAVING COUNT(*) > 0
    UNION ALL
    SELECT 'phase', phase_group, median(enrollment_count)
    FROM planned
    GROUP BY phase_group
)
SELECT rc.week_start, rc.week_end, 'median_planned_enrollment', u.dimension, u.dimension_value, u.value, rc.run_date, rc.is_backfill
FROM unioned u
CROSS JOIN rc;
