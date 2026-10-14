-- Weekly numbers. Run with:
--   npx wrangler d1 execute bvt-telemetry --remote --file stats.sql
-- "Active" = dictated or ran a session that week; "running" = the app pinged.
SELECT strftime('%Y-W%W', day)                     AS week,
       COUNT(DISTINCT id)                           AS running,
       COUNT(DISTINCT CASE WHEN dictations + meeting_sessions + phone_sessions > 0
                           THEN id END)             AS active,
       SUM(dictations)                              AS dictations,
       SUM(meeting_sessions + phone_sessions)       AS sessions,
       SUM(minutes)                                 AS minutes,
       SUM(failures)                                AS failures
FROM pings
GROUP BY week
ORDER BY week DESC
LIMIT 12;

-- New installs per week (first day an ID was seen)
SELECT strftime('%Y-W%W', first) AS week, COUNT(*) AS new_installs
FROM (SELECT id, MIN(day) AS first FROM pings GROUP BY id)
GROUP BY week ORDER BY week DESC LIMIT 12;

-- Last 7 days: versions, providers, countries
SELECT app, COUNT(DISTINCT id) AS installs FROM pings
WHERE day >= date('now', '-7 days') GROUP BY app ORDER BY installs DESC;
SELECT provider, COUNT(DISTINCT id) AS installs FROM pings
WHERE day >= date('now', '-7 days') GROUP BY provider ORDER BY installs DESC;
SELECT country, COUNT(DISTINCT id) AS installs FROM pings
WHERE day >= date('now', '-7 days') GROUP BY country ORDER BY installs DESC;
