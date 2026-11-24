-- Studies updated this week that are now terminated / withdrawn / suspended (detail for the report).
WITH rc AS (SELECT * FROM run_context)
SELECT s.nct_id, s.brief_title, s.overall_status, s.lead_sponsor_name, s.lead_sponsor_class,
       s.phase_group, s.enrollment_count, s.why_stopped
FROM studies_current s, rc
WHERE s.last_update_post_date BETWEEN rc.week_start AND rc.week_end
  AND s.overall_status IN ('TERMINATED', 'WITHDRAWN', 'SUSPENDED')
ORDER BY (s.lead_sponsor_class = 'INDUSTRY') DESC, s.enrollment_count DESC NULLS LAST, s.nct_id
LIMIT 8;
