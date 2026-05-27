-- Masking Sensitive Data in Firewall Logs
WITH FirewallLogs AS (
    SELECT 1 AS log_id, 'Connection blocked: Source IP: 10.0.0.1, Destination IP: 172.16.1.25, Port: 80, Protocol: TCP, Contact: +1-555-123-4567' AS log_entry
    UNION ALL
    SELECT 2 AS log_id, 'Connection allowed: Source IP: 192.168.1.100, Destination IP: 203.0.113.5, Port: 443, Protocol: TCP, Phone: 555-987-6543' AS log_entry
    UNION ALL
    SELECT 3 AS log_id, 'Outbound traffic: Source IP: 172.16.5.50, Destination IP: 8.8.8.8, Port: 53, Protocol: UDP, Number: (555) 111-2222' AS log_entry
    UNION ALL
    SELECT 4 AS log_id, 'Inbound traffic: Source IP: 1.2.3.4, Destination IP: 10.1.2.3, Port: 22, Protocol: TCP, Contact Support: 555-555-5555' AS log_entry
    UNION ALL
    SELECT 5 AS log_id, 'Firewall rule updated. Call 555-123-9999 for details. src: 10.10.10.10, dst: 199.199.199.199' AS log_entry
)
SELECT
    log_id,
    regexp_replace(ai_mask(log_entry, ARRAY['ip address', 'phone']), '(?s)^.*?</think>\s*', '') AS masked_log_entry
FROM FirewallLogs
;
