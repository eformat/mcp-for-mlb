-- Knowledge Graph: find semantically related articles and extract key technologies mentioned
WITH seed AS (
    SELECT embedding AS seed_vec, json_extract_scalar(cmetadata, '$.title') AS seed_title
    FROM vectordb.public.langchain_pg_embedding
    WHERE json_extract_scalar(cmetadata, '$.title') LIKE '%LLM%'
      AND length(document) > 200
    LIMIT 1
),
scored AS (
    SELECT
        json_extract_scalar(e.cmetadata, '$.title') AS title,
        substr(e.document, 1, 500) AS chunk,
        reduce(zip_with(e.embedding, s.seed_vec, (a, b) -> CAST(a AS double) * CAST(b AS double)),
               DOUBLE '0.0', (st, x) -> st + x, st -> st)
        / (sqrt(reduce(transform(e.embedding, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st))
         * sqrt(reduce(transform(s.seed_vec, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st)))
        AS similarity
    FROM vectordb.public.langchain_pg_embedding e
    CROSS JOIN seed s
    WHERE json_extract_scalar(e.cmetadata, '$.title') != s.seed_title
      AND length(e.document) > 300
      AND e.document NOT LIKE '%Anguilla%'
      AND e.document NOT LIKE '%Privacy statement%'
),
related AS (
    SELECT DISTINCT title, chunk, similarity
    FROM scored
    WHERE similarity < 0.999
    ORDER BY similarity DESC
    LIMIT 5
)
SELECT
    title,
    round(similarity, 4) AS score,
    CAST(ai_extract(chunk, ARRAY['product', 'technology', 'programming_language']) AS JSON) AS entities
FROM related;
