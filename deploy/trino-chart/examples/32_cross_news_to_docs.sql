-- Cross-Catalog: match negative tech news from S3 lakehouse to Red Hat developer KB articles in PostgreSQL
WITH tech_news AS (
    SELECT substr(text, 1, 300) AS headline,
           ai_classify(text, ARRAY['Cloud', 'Security', 'DevOps', 'Data', 'AI']) AS tech_area
    FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.05)
    WHERE label = 'negative'
      AND (text LIKE '%cloud%' OR text LIKE '%software%' OR text LIKE '%security%' OR text LIKE '%data%')
    LIMIT 3
),
kb_articles AS (
    SELECT DISTINCT
        json_extract_scalar(cmetadata, '$.title') AS title
    FROM vectordb.public.langchain_pg_embedding
    WHERE length(document) > 300
      AND document NOT LIKE '%Anguilla%'
      AND (json_extract_scalar(cmetadata, '$.title') LIKE '%OpenShift%'
           OR json_extract_scalar(cmetadata, '$.title') LIKE '%Kubernetes%'
           OR json_extract_scalar(cmetadata, '$.title') LIKE '%security%')
    LIMIT 10
)
SELECT
    n.headline,
    n.tech_area,
    k.title AS kb_article,
    ai_gen('In 2 sentences, explain how "' || k.title || '" could help address this challenge: ' || n.headline) AS insight
FROM tech_news n
CROSS JOIN (SELECT title FROM kb_articles LIMIT 1) k;
