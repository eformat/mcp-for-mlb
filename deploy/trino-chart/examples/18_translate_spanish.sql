-- Translating Spanish Intrusion Detection System (IDS) Alerts to English
WITH IDSEvents AS (
    SELECT * FROM (
        VALUES
            (1, '2024-10-27 11:00:00', 'IDS', '172.16.1.5', '203.0.113.10', 'TCP', '21', 'Alerta', 'Intento de explotación de FTP', 'Madrid'),
            (2, '2024-10-27 11:05:00', 'IDS', '172.16.1.6', '203.0.113.11', 'TCP', '25', 'Advertencia', 'Escaneo de puertos SMTP', 'Barcelona'),
            (3, '2024-10-27 11:10:00', 'IDS', '172.16.1.7', '203.0.113.12', 'UDP', '161', 'Alerta', 'Tráfico SNMP inusual', 'Valencia'),
            (4, '2024-10-27 11:15:00', 'IDS', '172.16.1.8', '203.0.113.13', 'TCP', '80', 'Advertencia', 'Posible ataque de cross-site scripting (XSS)', 'Sevilla'),
            (5, '2024-10-27 11:20:00', 'IDS', '172.16.1.9', '203.0.113.14', 'TCP', '445', 'Alerta', 'Actividad de SMB sospechosa', 'Zaragoza')
    ) AS t (id, log_time, device, src_ip, dest_ip, protocol, port, alert_level, description, location)
),
TranslatedIDSEvents AS (
    SELECT
        id,
        log_time,
        regexp_replace(ai_translate(device, 'en'), '(?s)^.*?</think>\s*', '') AS device_en,
        src_ip,
        dest_ip,
        protocol,
        port,
        regexp_replace(ai_translate(alert_level, 'en'), '(?s)^.*?</think>\s*', '') AS alert_level_en,
        regexp_replace(ai_translate(description, 'en'), '(?s)^.*?</think>\s*', '') AS description_en,
        regexp_replace(ai_translate(location,'en'), '(?s)^.*?</think>\s*', '') as location_en
    FROM IDSEvents
)
SELECT * FROM TranslatedIDSEvents
;
