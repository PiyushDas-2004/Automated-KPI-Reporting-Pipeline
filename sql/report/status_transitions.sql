-- Confirmed (SCD2) status transitions detected this week, as a from -> to matrix.
WITH rc AS (SELECT * FROM run_context)
SELECT previous_status, overall_status AS new_status, COUNT(*) AS trials
FROM study_status_history, rc
WHERE detected_week_start = rc.week_start AND previous_status IS NOT NULL
GROUP BY ALL
ORDER BY trials DESC, previous_status, new_status
LIMIT 10;
