-- Cross-Catalog: federated intelligence report combining S3 financial news + PostgreSQL knowledge base
WITH negative_news AS (
    SELECT text
    FROM lakehouse.finance.financial_news TABLESAMPLE BERNOULLI (0.5)
    WHERE label = 'negative'
      AND (text LIKE '%technology%' OR text LIKE '%software%' OR text LIKE '%cloud%' OR text LIKE '%cyber%')
    LIMIT 5
),
kb_highlights AS (
    SELECT DISTINCT
        json_extract_scalar(cmetadata, '$.title') AS title,
        json_extract_scalar(cmetadata, '$.description') AS description
    FROM vectordb.public.langchain_pg_embedding
    WHERE (json_extract_scalar(cmetadata, '$.title') LIKE '%OpenShift%'
           OR json_extract_scalar(cmetadata, '$.title') LIKE '%security%'
           OR json_extract_scalar(cmetadata, '$.title') LIKE '%Kubernetes%')
      AND length(document) > 300
      AND document NOT LIKE '%Anguilla%'
    LIMIT 5
)
SELECT ai_gen(
    'You are a technology strategist. Write a 1-page intelligence briefing that connects these two data sources.' ||
    ' First, here are negative financial news headlines about tech companies: ' ||
    (SELECT json_format(CAST(array_agg(substr(text, 1, 300)) AS JSON)) FROM negative_news) ||
    ' Second, here are Red Hat developer knowledge base articles that represent solutions: ' ||
    (SELECT json_format(CAST(array_agg(title || ': ' || COALESCE(description, '')) AS JSON)) FROM kb_highlights) ||
    ' Connect the market risks to the technology solutions. Format as: Executive Summary, Key Risks, Recommended Solutions, Action Items.'
) AS intelligence_briefing;
