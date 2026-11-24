-- =============================================================================
-- 40_kpi_reset.sql - make the KPI refresh idempotent: drop this week's rows
-- before recomputing them (delete + insert inside one transaction).
-- =============================================================================
DELETE FROM kpi_weekly
WHERE week_start = (SELECT week_start FROM run_context);
