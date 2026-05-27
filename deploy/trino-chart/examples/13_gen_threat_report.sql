-- Threat Report Summary Generation
WITH security_logs AS (
    SELECT * FROM (
      VALUES
          (1, '2024-07-27T10:00:00Z', '192.168.1.10', 'user1', 'Failed Login', 'Brute force attempt detected'),
          (2, '2024-07-27T10:15:00Z', '10.0.0.5', 'system', 'Port Scan', 'Multiple connections to closed ports'),
          (3, '2024-07-27T10:30:00Z', '192.168.1.10', 'user1', 'Successful Login', 'Login after multiple failures'),
          (4, '2024-07-27T11:00:00Z', '203.0.113.25', 'unknown', 'Data Exfiltration', 'Large outbound data transfer'),
          (5, '2024-07-27T11:45:00Z', '192.168.1.20', 'user2', 'Malware Detected', 'Trojan.GenericKD.12345 detected')
  ) AS t(log_id, timestamp, ip_address, user_id, event_type, description)
)
SELECT regexp_replace(ai_gen(
   'Generate a concise security threat report summary based on the following SIEM log entries, include severity and recommendations: ' ||
   (SELECT json_format(CAST(array_agg(CAST(map(
      ARRAY['timestamp', 'ip_address', 'user', 'event_type', 'description'],
      ARRAY[timestamp, ip_address, user_id, event_type, description]
      ) AS JSON)) AS JSON))
        FROM security_logs)
       ), '(?s)^.*?</think>\s*', '') AS threat_report
;
