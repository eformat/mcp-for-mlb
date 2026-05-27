-- Log Anomaly Description and Explanation
WITH firewall_logs AS (
    SELECT * FROM (
      VALUES
          (1, '2024-07-27T12:00:00Z', '192.168.1.1', '8.8.8.8', '443', 'ALLOW'),
          (2, '2024-07-27T12:00:05Z', '192.168.1.1', '8.8.8.8', '443', 'ALLOW'),
          (3, '2024-07-27T12:00:10Z', '192.168.1.1', '8.8.8.8', '443', 'ALLOW'),
          (4, '2024-07-27T12:00:15Z', '192.168.1.1', '172.217.160.142', '443', 'ALLOW'),
          (5, '2024-07-27T12:00:20Z', '192.168.1.1', '8.8.8.8', '443', 'ALLOW'),
          (6, '2024-07-27T12:00:20Z', '192.168.1.1', '172.217.160.142', '443', 'ALLOW'),
          (7, '2024-07-27T12:00:20Z', '192.168.1.1', '8.8.8.8', '443', 'ALLOW')
  ) AS t(log_id, timestamp, source_ip, dest_ip, dest_port, action)
),
     anomaly_log AS (
         SELECT * FROM firewall_logs WHERE dest_ip = '172.217.160.142'
     )
SELECT regexp_replace(ai_gen(
               'Describe the potential security issue represented by this unusual firewall log and explain why it might be a concern, given the context of frequent allowed connections to 8.8.8.8:443. Log details: ' ||
               (SELECT CAST(json_format(CAST(array_agg(row(timestamp, source_ip, dest_ip, dest_port, action)) AS JSON)) AS VARCHAR) FROM anomaly_log
               )
       ), '(?s)^.*?</think>\s*', '') AS anomaly_explanation
;
