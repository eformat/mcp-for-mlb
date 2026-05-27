-- Multilingual Knowledge Base: find similar articles and translate on-the-fly
WITH seed AS (
    SELECT embedding AS seed_vec, json_extract_scalar(cmetadata, '$.title') AS seed_title
    FROM vectordb.public.langchain_pg_embedding
    WHERE json_extract_scalar(cmetadata, '$.title') LIKE '%container%'
    LIMIT 1
),
similar_docs AS (
    SELECT DISTINCT
        json_extract_scalar(e.cmetadata, '$.title') AS title,
        substr(e.document, 1, 300) AS chunk,
        reduce(zip_with(e.embedding, s.seed_vec, (a, b) -> CAST(a AS double) * CAST(b AS double)),
               DOUBLE '0.0', (st, x) -> st + x, st -> st)
        / (sqrt(reduce(transform(e.embedding, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st))
         * sqrt(reduce(transform(s.seed_vec, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st)))
        AS similarity
    FROM vectordb.public.langchain_pg_embedding e
    CROSS JOIN seed s
    WHERE json_extract_scalar(e.cmetadata, '$.title') != s.seed_title
    ORDER BY similarity DESC
    LIMIT 3
)
SELECT
    title,
    round(similarity, 4) AS score,
    ai_translate(chunk, 'ja') AS japanese,
    ai_translate(chunk, 'ko') AS korean
FROM similar_docs;
