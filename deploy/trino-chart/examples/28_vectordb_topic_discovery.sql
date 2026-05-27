-- Topic Discovery: classify random knowledge base chunks into Red Hat product areas
WITH sample_chunks AS (
    SELECT
        json_extract_scalar(cmetadata, '$.title') AS title,
        substr(document, 1, 500) AS chunk
    FROM vectordb.public.langchain_pg_embedding TABLESAMPLE BERNOULLI (0.01)
    WHERE length(document) > 100
    LIMIT 8
)
SELECT
    title,
    ai_classify(chunk, ARRAY['OpenShift', 'RHEL', 'Quarkus', 'Ansible', 'Middleware', 'AI/ML', 'Security', 'Developer Tools', 'Kubernetes', 'Other']) AS topic
FROM sample_chunks;
