-- Market Mood Ring: cross-validate AI vs human sentiment labels, explain disagreements
WITH sample AS (
    SELECT text, label AS human_label,
           ai_analyze_sentiment(text) AS ai_sentiment
    FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.05)
    LIMIT 5
)
SELECT substr(text, 1, 80) AS snippet,
       human_label, ai_sentiment,
       CASE WHEN human_label != ai_sentiment THEN 'DISAGREE' ELSE 'AGREE' END AS verdict,
       ai_gen('In one sentence, explain why this financial news might be seen as ' || ai_sentiment || ': ' || substr(text, 1, 500)) AS reasoning
FROM sample;
