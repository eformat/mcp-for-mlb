-- Daily Briefing: aggregate negative financial news into an analyst morning report
WITH bad_news AS (
    SELECT text, source FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.5)
    WHERE label = 'negative' LIMIT 10
)
SELECT ai_gen(
    'You are a senior financial analyst. Write a concise morning briefing (5 bullet points max) summarizing the key risks and themes from these financial news articles. Include actionable recommendations: ' ||
    (SELECT json_format(CAST(array_agg(text) AS JSON)) FROM bad_news)
) AS morning_briefing;
