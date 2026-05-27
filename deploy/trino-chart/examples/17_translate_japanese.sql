-- Translating Japanese Firewall Logs to English
WITH FirewallLogs AS (
    SELECT * FROM (
        VALUES
            (1, '2024-10-27 10:00:00', 'ファイアウォール', '192.168.1.1', '10.0.0.5', 'TCP', '80', '許可', '外部からの接続', '東京'),
            (2, '2024-10-27 10:05:00', 'ファイアウォール', '192.168.1.1', '10.0.0.6', 'UDP', '53', '拒否', '不正なDNSクエリ', '大阪'),
            (3, '2024-10-27 10:10:00', 'ファイアウォール', '192.168.1.2', '10.0.0.7', 'TCP', '443', '許可', 'SSL通信', '名古屋'),
            (4, '2024-10-27 10:15:00', 'ファイアウォール', '192.168.1.3', '10.0.0.8', 'TCP', '22', '拒否', 'SSHブルートフォース', '福岡'),
            (5, '2024-10-27 10:20:00', 'ファイアウォール', '192.168.1.4', '10.0.0.9', 'TCP', '8080', '許可', 'プロキシ経由', '札幌')
    ) AS t (id, log_time, device, src_ip, dest_ip, protocol, port, action, description, location)
),
TranslatedLogs AS (
    SELECT
        id,
        log_time,
        regexp_replace(ai_translate(device, 'en'), '(?s)^.*?</think>\s*', '') as device_en,
        src_ip,
        dest_ip,
        protocol,
        port,
        regexp_replace(ai_translate(action, 'en'), '(?s)^.*?</think>\s*', '') AS action_en,
        regexp_replace(ai_translate(description, 'en'), '(?s)^.*?</think>\s*', '') AS description_en,
        regexp_replace(ai_translate(location, 'en'), '(?s)^.*?</think>\s*', '') AS location_en
    FROM FirewallLogs
)
SELECT * FROM TranslatedLogs
;
