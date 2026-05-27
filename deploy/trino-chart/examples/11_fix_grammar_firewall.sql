-- Correcting Grammar in Firewall Logs for Better Reporting
WITH FirewallLogs AS (
    SELECT * FROM (
        VALUES
        (1, 'Firewall denyed incomming conection from 192.168.1.100 to port 80'),
        (2, 'Acess granted to 10.0.0.5, port 443, conection stablished.'),
        (3, 'Connection timeout; user tried login too server.'),
        (4, 'Port scan detectted from IP adress 172.16.0.23.'),
        (5, 'Firewall rule updated; new rule block all trafic.')
    ) AS t (log_id, log_message)
),
CorrectedLogs AS (
    SELECT log_id, regexp_replace(ai_fix_grammar(log_message), '(?s)^.*?</think>\s*', '') AS corrected_message
    FROM FirewallLogs
)
SELECT * FROM CorrectedLogs
;
