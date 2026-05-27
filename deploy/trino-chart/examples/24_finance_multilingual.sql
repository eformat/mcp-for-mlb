-- Multilingual Trading Desk: translate financial news and verify sentiment survives
WITH articles AS (
    SELECT text FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.05)
    WHERE label = 'positive' LIMIT 3
)
SELECT substr(text, 1, 60) AS original,
       ai_analyze_sentiment(text) AS en_sentiment,
       ai_translate(substr(text, 1, 300), 'ja') AS japanese,
       ai_translate(substr(text, 1, 300), 'es') AS spanish,
       ai_translate(substr(text, 1, 300), 'ko') AS korean
FROM articles;
