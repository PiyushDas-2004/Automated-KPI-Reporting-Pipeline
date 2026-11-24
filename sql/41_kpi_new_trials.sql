-- =============================================================================
-- 41_kpi_new_trials.sql - new registrations this week: total, by phase,
-- by sponsor class, by study type and the top 10 conditions.
-- =============================================================================
INSERT INTO kpi_weekly (week_start, week_end, kpi_name, dimension, dimension_value, value, run_date, is_backfill)
WITH rc AS (
    SELECT * FROM run_context
),
new_trials AS (
    SELECT s.*
    FROM studies_current s
    CROSS JOIN rc
    WHERE s.first_post_date BETWEEN rc.week_start AND rc.week_end
),
by_phase AS (
    -- LEFT JOIN from the dimension so a phase with no new trials reports 0, not a gap
    SELECT 'phase' AS dimension, d.phase_group AS dimension_value, COUNT(n.nct_id) AS value
    FROM dim_phase_group d
    LEFT JOIN new_trials n ON n.phase_group = d.phase_group
    GROUP BY d.phase_group
),
by_sponsor_class AS (
    SELECT 'sponsor_class', d.sponsor_class, COUNT(n.nct_id)
    FROM dim_sponsor_class d
    LEFT JOIN new_trials n ON n.lead_sponsor_class = d.sponsor_class
    GROUP BY d.sponsor_class
),
by_study_type AS (
    SELECT 'study_type', COALESCE(study_type, 'UNKNOWN'), COUNT(*)
    FROM new_trials
    GROUP BY ALL
),
condition_counts AS (
    -- case-insensitive grouping; display the most common spelling
    SELECT
        lower(trim(c.condition))         AS condition_key,
        mode(trim(c.condition))          AS condition_label,
        COUNT(DISTINCT n.nct_id)         AS trials
    FROM new_trials n, UNNEST(n.conditions) AS c(condition)
    GROUP BY 1
),
top_conditions AS (
    SELECT 'condition_top10', condition_label, trials
    FROM condition_counts
    QUALIFY ROW_NUMBER() OVER (ORDER BY trials DESC, condition_key) <= 10
),
total AS (
    SELECT 'total' AS dimension, 'All' AS dimension_value, COUNT(*) AS value FROM new_trials
),
industry_share AS (
    SELECT 'total' AS dimension, 'All' AS dimension_value,
           ROUND(100.0 * COUNT(*) FILTER (WHERE lead_sponsor_class = 'INDUSTRY') / NULLIF(COUNT(*), 0), 1) AS value
    FROM new_trials
),
unioned AS (
    SELECT 'new_trials' AS kpi_name, * FROM total
    UNION ALL SELECT 'new_trials', * FROM by_phase
    UNION ALL SELECT 'new_trials', * FROM by_sponsor_class
    UNION ALL SELECT 'new_trials', * FROM by_study_type
    UNION ALL SELECT 'new_trials', * FROM top_conditions
    UNION ALL SELECT 'industry_share_new_trials_pct', * FROM industry_share
)
SELECT rc.week_start, rc.week_end, u.kpi_name, u.dimension, u.dimension_value, u.value, rc.run_date, rc.is_backfill
FROM unioned u
CROSS JOIN rc;
