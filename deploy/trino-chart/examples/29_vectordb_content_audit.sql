-- Content Quality Audit: analyze sentiment and fix grammar on knowledge base articles
WITH sample_chunks AS (
    SELECT
        json_extract_scalar(cmetadata, '$.title') AS title,
        substr(document, 1, 300) AS chunk
    FROM vectordb.public.langchain_pg_embedding TABLESAMPLE BERNOULLI (0.01)
    WHERE length(document) > 300
      AND document NOT LIKE '%Anguilla%'
      AND document NOT LIKE '%Privacy statement%'
      AND document NOT LIKE '%Skip to main content%'
    LIMIT 4
)
SELECT
    title,
    ai_analyze_sentiment(chunk) AS tone,
    ai_fix_grammar(chunk) AS improved,
    ai_classify(chunk, ARRAY['Tutorial', 'Reference', 'Blog Post', 'Release Notes', 'Video Transcript']) AS content_type
FROM sample_chunks;
