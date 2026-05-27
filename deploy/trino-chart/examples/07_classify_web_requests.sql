-- Classifying Web Requests (URL Categorization)
WITH web_requests AS (
    SELECT *
    FROM (VALUES
    (1, '/admin.php?id=1;SELECT * FROM users', '2024-07-27 10:00:00', '192.0.2.1'),
    (2, '/blog/post.php?id=123', '2024-07-27 10:01:00', '192.0.2.5'),
    (3, '<script>alert("XSS")</script>', '2024-07-27 10:02:00', '192.0.2.10'),
    (4, '/login.php', '2024-07-27 10:03:00', '192.0.2.15'),
    (5, '/products.php?category=electronics'' OR ''1''=''1', '2024-07-27 10:04:00', '192.0.2.20'),
    (6, '/contact.php', '2024-07-27 10:05:00', '192.0.2.25')
    ) AS t (request_id, url, timestamp, source_ip)
    )
SELECT
    request_id,
    url,
    regexp_replace(ai_classify(url, ARRAY['SQL Injection', 'Cross-Site Scripting (XSS)', 'Normal Traffic', 'Path Traversal', 'Command Injection']), '(?s)^.*?</think>\s*', '') AS attack_type
FROM web_requests
;
