-- =============================================================================
-- 50_kpi_trend.sql - week-over-week change and 4-week rolling average for
-- EVERY KPI series (kpi_name x dimension x dimension_value).
--   * prev_week_value is only filled when the previous row is exactly 7 days
--     earlier, so a gap (e.g. a condition that dropped out of the top 10) never
--     masquerades as last week's value.
--   * the rolling window is a RANGE over dates, so it averages the weeks that
--     exist inside the trailing 28 days (current week + 3 before).
-- =============================================================================
CREATE OR REPLACE VIEW kpi_trend AS
WITH ordered AS (
    SELECT
        k.*,
        LAG(value)      OVER w AS lag_value,
        LAG(week_start) OVER w AS lag_week_start,
        AVG(value) OVER (
            PARTITION BY kpi_name, dimension, dimension_value
            ORDER BY week_start
            RANGE BETWEEN INTERVAL 21 DAYS PRECEDING AND CURRENT ROW
        ) AS rolling_4wk_avg,
        COUNT(*) OVER (
            PARTITION BY kpi_name, dimension, dimension_value
            ORDER BY week_start
            RANGE BETWEEN INTERVAL 21 DAYS PRECEDING AND CURRENT ROW
        ) AS weeks_in_rolling_avg
    FROM kpi_weekly k
    WINDOW w AS (PARTITION BY kpi_name, dimension, dimension_value ORDER BY week_start)
)
SELECT
    week_start,
    week_end,
    kpi_name,
    dimension,
    dimension_value,
    value,
    CASE WHEN lag_week_start = week_start - INTERVAL 7 DAYS THEN lag_value END            AS prev_week_value,
    CASE WHEN lag_week_start = week_start - INTERVAL 7 DAYS THEN value - lag_value END    AS wow_change,
    CASE WHEN lag_week_start = week_start - INTERVAL 7 DAYS AND lag_value <> 0
         THEN ROUND((value - lag_value) / lag_value * 100, 1) END                          AS wow_pct,
    ROUND(rolling_4wk_avg, 2)                                                              AS rolling_4wk_avg,
    weeks_in_rolling_avg,
    run_date,
    is_backfill
FROM ordered;
