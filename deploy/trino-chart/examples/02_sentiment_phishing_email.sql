-- Phishing Email Detection
WITH email_data AS (
    SELECT
        'urgent_account_issue@example.com' AS sender,
        'Your account has been compromised!  Click here IMMEDIATELY to avoid permanent suspension.  Failure to act will result in data loss!' AS email_body
    UNION ALL
    SELECT
        'support@legitcompany.com' AS sender,
        'We are writing to inform you about an upcoming scheduled maintenance window.  There may be brief interruptions to service.' AS email_body
    UNION ALL
    SELECT
        'newsletter@company.com' AS sender,
        'We are excited to announce our new product line!  Check out the amazing features and special offers.' AS email_body
)
SELECT
    sender,
    email_body,
    regexp_replace(ai_analyze_sentiment(email_body), '(?s)^.*?</think>\s*', '') AS sentiment
FROM email_data
;
