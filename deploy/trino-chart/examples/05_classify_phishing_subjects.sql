-- Identifying Phishing Email Subjects
WITH email_subjects AS (
    SELECT 'URGENT: Your account has been compromised!' AS subject
    UNION ALL
    SELECT 'You have won a free iPhone! Claim your prize now!' AS subject
    UNION ALL
    SELECT 'Meeting Reminder: Project Kickoff' AS subject
    UNION ALL
    SELECT 'Your Bank of America account requires immediate verification' AS subject
    UNION ALL
    SELECT 'Newsletter - July Edition' AS subject
)
SELECT
    subject,
    regexp_replace(ai_classify(subject, ARRAY['Phishing', 'Spam', 'Legitimate']), '(?s)^.*?</think>\s*', '') AS classification
FROM email_subjects
;
