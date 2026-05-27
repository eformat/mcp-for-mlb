-- Cross-Catalog: classify hotel complaints then find analogous software architecture lessons from the KB
WITH complaints AS (
    SELECT review
    FROM lakehouse.reviews.hotel_reviews TABLESAMPLE BERNOULLI (0.1)
    WHERE label = 'NEGATIVE'
    LIMIT 3
),
classified AS (
    SELECT
        substr(review, 1, 150) AS complaint,
        ai_classify(review, ARRAY['Poor Service', 'Dirty Room', 'Bad Location', 'Overpriced', 'Noisy', 'Bad Food']) AS issue_type
    FROM complaints
),
kb_match AS (
    SELECT DISTINCT
        c.complaint,
        c.issue_type,
        json_extract_scalar(e.cmetadata, '$.title') AS kb_article
    FROM classified c
    JOIN vectordb.public.langchain_pg_embedding e
        ON json_extract_scalar(e.cmetadata, '$.title') LIKE '%architecture%'
           OR json_extract_scalar(e.cmetadata, '$.title') LIKE '%design pattern%'
    WHERE length(e.document) > 300
      AND e.document NOT LIKE '%Anguilla%'
    LIMIT 3
)
SELECT
    complaint,
    issue_type,
    kb_article,
    ai_gen('Draw a creative one-paragraph analogy between this hotel complaint: "' || complaint || '" (issue: ' || issue_type || ') and a software architecture lesson from "' || kb_article || '". Make it witty and educational.') AS analogy
FROM kb_match;
