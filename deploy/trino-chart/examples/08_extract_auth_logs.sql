-- Extracting Usernames and IP Addresses from Authentication Logs
WITH AuthenticationLogs AS (
    SELECT * FROM (VALUES
        ('2024-10-27 10:00:00 User jdoe logged in successfully from 192.168.1.100'),
        ('2024-10-27 10:01:00 User asmith login failed from 10.0.0.5'),
        ('2024-10-27 10:05:00 Authentication failure for user bbrown from 203.0.113.25'),
        ('2024-10-27 10:10:00 User jdoe logged in successfully from 192.168.1.100'),
        ('2024-10-27 10:12:00 User cjones accessed resource from 172.16.0.23, login successful'),
        ('2024-10-27 10:15:00 System event: User password reset requested by admin for user: djohnson from IP: 192.168.1.5'),
        ('2024-10-27 10:18:00 User evilhacker attempted login from 1.2.3.4')
    ) AS t (log_entry)
)
SELECT
    log_entry,
    CAST(ai_extract(log_entry, ARRAY['username', 'ip_address']) AS JSON) AS extracted_data
FROM AuthenticationLogs
;
