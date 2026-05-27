-- Extracting File Paths and Actions from File Integrity Monitoring (FIM) Logs
WITH FIMLogs AS (
    SELECT * FROM (VALUES
        ('2024-10-27 11:00:00 File /etc/passwd was modified'),
        ('2024-10-27 11:02:00 File /var/log/syslog was created'),
        ('2024-10-27 11:05:00 File /home/user/documents/report.docx was deleted'),
        ('2024-10-27 11:08:00 File /tmp/temp_script.sh was accessed'),
        ('2024-10-27 11:10:00 File /usr/bin/malware was created'),
        ('2024-10-27 11:12:00 Attempt to modify file /etc/shadow failed')
    ) AS t (log_entry)
)
SELECT
    log_entry,
    CAST(ai_extract(log_entry, ARRAY['file_path', 'action']) AS JSON) AS extracted_data
FROM FIMLogs
;
