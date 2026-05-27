-- Financial Threat Intelligence: classify risk type, extract entities, mask PII
SELECT substr(text, 1, 80) AS headline,
       ai_classify(text, ARRAY['Market Risk', 'Regulatory Risk', 'Credit Risk', 'Operational Risk', 'No Risk']) AS risk_type,
       CAST(ai_extract(text, ARRAY['company', 'ticker_symbol', 'dollar_amount']) AS JSON) AS entities,
       ai_mask(text, ARRAY['person name', 'email', 'phone number']) AS sanitized
FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.05)
WHERE label = 'negative'
LIMIT 4;
