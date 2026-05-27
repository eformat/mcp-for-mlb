-- PII-Safe Multilingual Export: mask PII + fix grammar + translate hotel reviews from S3
WITH sample_reviews AS (
    SELECT review
    FROM lakehouse.reviews.hotel_reviews
    TABLESAMPLE BERNOULLI (0.1)
    LIMIT 4
)
SELECT
    substr(review, 1, 60) AS original_snippet,
    ai_mask(review, ARRAY['person name', 'email', 'phone number', 'credit card']) AS masked,
    ai_fix_grammar(review) AS corrected,
    ai_translate(review, 'es') AS spanish
FROM sample_reviews;
