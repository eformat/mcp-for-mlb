-- Categorizing Security Alerts
WITH siem_alerts AS (
    SELECT 'Alert: Trojan.Generic detected on host server123' AS alert_description
    UNION ALL
    SELECT 'Alert: Unusual large file upload to external cloud storage' AS alert_description
    UNION ALL
    SELECT 'Alert: User account login from multiple geolocations within 1 hour' AS alert_description
    UNION ALL
    SELECT 'Alert: User downloaded large number of documents from sensitive share' AS alert_description
    UNION ALL
    SELECT 'Alert: Application crash reported on user workstation' as alert_description
)
SELECT
    alert_description,
    regexp_replace(ai_classify(alert_description, ARRAY['Malware', 'Data Exfiltration', 'Credential Compromise', 'Insider Threat', 'Application Error']), '(?s)^.*?</think>\s*', '') AS threat_category
FROM siem_alerts
;
