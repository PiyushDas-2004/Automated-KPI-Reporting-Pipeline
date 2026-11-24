-- =============================================================================
-- 44_kpi_sites_by_country.sql - trial sites added by this week's new registrations,
-- total and top 10 countries (ranked with a window function).
-- =============================================================================
INSERT INTO kpi_weekly (week_start, week_end, kpi_name, dimension, dimension_value, value, run_date, is_backfill)
WITH rc AS (
    SELECT * FROM run_context
),
sites AS (
    SELECT s.nct_id, trim(c.country) AS country
    FROM studies_current s
    CROSS JOIN rc,
    UNNEST(s.location_countries) AS c(country)
    WHERE s.first_post_date BETWEEN rc.week_start AND rc.week_end
),
by_country AS (
    SELECT
        country,
        COUNT(*)                                             AS sites,
        RANK() OVER (ORDER BY COUNT(*) DESC)                 AS site_rank,
        ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, country)  AS rn
    FROM sites
    GROUP BY country
),
unioned AS (
    SELECT 'new_trial_sites' AS kpi_name, 'total' AS dimension, 'All' AS dimension_value, COUNT(*)::DOUBLE AS value FROM sites
    UNION ALL
    SELECT 'countries_with_new_sites', 'total', 'All', COUNT(DISTINCT country) FROM sites
    UNION ALL
    SELECT 'new_trial_sites', 'country_top10', country, sites FROM by_country WHERE rn <= 10
)
SELECT rc.week_start, rc.week_end, u.kpi_name, u.dimension, u.dimension_value, u.value, rc.run_date, rc.is_backfill
FROM unioned u
CROSS JOIN rc;
