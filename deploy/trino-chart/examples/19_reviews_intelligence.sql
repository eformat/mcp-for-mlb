-- Review Intelligence Pipeline: sentiment + classify + extract on hotel reviews from S3
WITH sample_reviews AS (
    SELECT review, label
    FROM lakehouse.reviews.hotel_reviews
    TABLESAMPLE BERNOULLI (0.1)
    LIMIT 4
)
SELECT
    substr(review, 1, 80) AS review_snippet,
    label AS original_label,
    ai_analyze_sentiment(review) AS sentiment,
    ai_classify(review, ARRAY['Service', 'Cleanliness', 'Location', 'Value', 'Amenities']) AS category,
    CAST(ai_extract(review, ARRAY['hotel_feature', 'issue']) AS JSON) AS extracted
FROM sample_reviews;
