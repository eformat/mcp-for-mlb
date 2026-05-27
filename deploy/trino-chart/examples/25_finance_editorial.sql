-- AI Editorial Pipeline: fix grammar, generate Bloomberg-style headline, classify sector
SELECT substr(text, 1, 60) AS raw_snippet,
       ai_fix_grammar(substr(text, 1, 500)) AS edited,
       ai_gen('Write a concise one-line Bloomberg-style headline for this article: ' || substr(text, 1, 500)) AS headline,
       ai_classify(text, ARRAY['Technology', 'Energy', 'Healthcare', 'Finance', 'Consumer', 'Industrial']) AS sector
FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.05)
LIMIT 4;
