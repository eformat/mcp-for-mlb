-- Insider Threat Detection (Messaging Platform)
WITH message_data AS (
    SELECT
    'user123' AS user_id,
    'This project is a complete disaster!  I''m so frustrated with management.  I''m thinking of taking my talents elsewhere.' AS message_text
    UNION ALL
    SELECT
    'user456' AS user_id,
    'Great work team!  Let''s keep up the momentum.' AS message_text
    UNION ALL
    SELECT
    'user789' AS user_id,
    'I just downloaded all the customer data.  Don''t tell anyone.' AS message_text
    UNION ALL
    SELECT
    'user789' AS user_id,
    'I can not download all the customer data.  Please provide assistance.' AS message_text
    )
SELECT
    user_id,
    message_text,
    regexp_replace(ai_analyze_sentiment(message_text), '(?s)^.*?</think>\s*', '') AS sentiment
FROM message_data
;
