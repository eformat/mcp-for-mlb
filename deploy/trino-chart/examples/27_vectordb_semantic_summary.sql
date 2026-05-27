-- Semantic Search + AI Summary: find related articles by cosine similarity, generate a reading list
WITH seed AS (
    SELECT embedding AS seed_vec, json_extract_scalar(cmetadata, '$.title') AS seed_title
    FROM vectordb.public.langchain_pg_embedding
    WHERE json_extract_scalar(cmetadata, '$.title') LIKE '%LLM%'
    LIMIT 1
),
similar_docs AS (
    SELECT DISTINCT
        json_extract_scalar(e.cmetadata, '$.title') AS title,
        json_extract_scalar(e.cmetadata, '$.source') AS url,
        reduce(zip_with(e.embedding, s.seed_vec, (a, b) -> CAST(a AS double) * CAST(b AS double)),
               DOUBLE '0.0', (st, x) -> st + x, st -> st)
        / (sqrt(reduce(transform(e.embedding, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st))
         * sqrt(reduce(transform(s.seed_vec, x -> CAST(x AS double) * CAST(x AS double)), DOUBLE '0.0', (st, x) -> st + x, st -> st)))
        AS similarity
    FROM vectordb.public.langchain_pg_embedding e
    CROSS JOIN seed s
    WHERE json_extract_scalar(e.cmetadata, '$.title') != s.seed_title
    ORDER BY similarity DESC
    LIMIT 5
)
SELECT ai_gen(
    'Create a concise recommended reading list from these related developer articles. For each, write one sentence on what the reader will learn: ' ||
    (SELECT json_format(CAST(array_agg(title || ' (' || url || ')') AS JSON)) FROM similar_docs)
) AS reading_list;
