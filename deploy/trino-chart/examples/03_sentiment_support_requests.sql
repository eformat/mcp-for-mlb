-- Detecting urgency or distress in support requests.
WITH support_requests AS (
SELECT 'userA' as user_id, 'I am locked out of my account and I cannot access critical systems. This is extremely urgent!' as request
UNION ALL
SELECT 'userB' as user_id, 'How do I reset my password' as request
)
SELECT user_id, request, regexp_replace(ai_analyze_sentiment(request), '(?s)^.*?</think>\s*', '') AS sentiment
FROM support_requests
;
