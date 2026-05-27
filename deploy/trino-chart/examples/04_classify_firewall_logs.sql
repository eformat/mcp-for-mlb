-- Classifying Firewall Log Entries
WITH firewall_logs AS (
    SELECT 'Blocked connection from 192.168.1.100 to port 22, repeated attempts' AS log_entry
    UNION ALL
    SELECT 'User jdoe accessed restricted file share /finance/budgets' AS log_entry
    UNION ALL
    SELECT 'Large outbound data transfer to 10.0.0.5 detected' AS log_entry
    UNION ALL
    SELECT 'Successful VPN connection from user asmith' AS log_entry
    UNION ALL
    SELECT 'Multiple failed login attempts for user bsmith from external IP' as log_entry
)
SELECT
    log_entry,
    regexp_replace(ai_classify(log_entry, ARRAY['Attack Attempt', 'Policy Violation', 'Network Scan', 'Legitimate Traffic']), '(?s)^.*?</think>\s*', '') AS classification
FROM firewall_logs
;
