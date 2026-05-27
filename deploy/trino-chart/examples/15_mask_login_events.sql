-- Masking PII in User Login Events
WITH LoginEvents AS (
    SELECT 1 AS id, 'User John Doe logged in from 192.168.1.10. Employee ID: EMP12345, Email: john.doe@example.com' AS event_description
    UNION ALL
    SELECT 2 AS id, 'Failed login attempt for Jane Smith (EMP67890) from 203.0.113.25. Email: jane.smith@example.com' AS event_description
    UNION ALL
    SELECT 3 AS id, 'Password reset requested by user Bob Williams, EMP11223, IP: 10.0.0.5, Email: bob.williams@example.com' AS event_description
    UNION ALL
    SELECT 4 AS id, 'Access granted to EMP44556 (Alice Brown) from 172.16.0.100. Email: alice.brown@anotherdomain.net' AS event_description
    UNION ALL
    SELECT 5 AS id, 'EMP99999, David Lee, tried accessing restricted resource from 192.168.1.200, Email: david.lee@company.org' AS event_description
)
SELECT
    id,
    regexp_replace(ai_mask(event_description, ARRAY['employee id', 'ip address', 'email']), '(?s)^.*?</think>\s*', '') AS masked_event_description
FROM LoginEvents
;
