-- Improving Clarity of Intrusion Detection System (IDS) Alerts
WITH IDSALerts AS (
    SELECT * FROM (
        VALUES
        (101, 'SQL injecton atempt detected; malicious payload found.'),
        (102, 'Cross-site scripting (XSS) vulnerability exploitted; user data compromised.'),
        (103, 'Brute force atack on SSH; multiple failed login attempts.'),
        (104, 'Malware donwloaded; file identified as trojan.'),
        (105, 'Data exfiltration attemp; large data transfer to unkown IP.')
    ) AS t (alert_id, alert_message)
),
CorrectedAlerts AS (
    SELECT alert_id, regexp_replace(ai_fix_grammar(alert_message), '(?s)^.*?</think>\s*', '') AS corrected_alert
    FROM IDSALerts
)
SELECT * FROM CorrectedAlerts
;
