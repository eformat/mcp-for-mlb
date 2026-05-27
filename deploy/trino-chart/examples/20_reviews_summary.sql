-- Guest Feedback Summary: aggregate reviews then generate executive report with ai_gen
WITH sample_reviews AS (
    SELECT review
    FROM lakehouse.reviews.hotel_reviews TABLESAMPLE BERNOULLI (0.5)
    WHERE label = 'NEGATIVE'
    LIMIT 5
)
SELECT ai_gen(
    'Generate a concise executive summary of the top guest complaints and recommend 3 actionable improvements based on these hotel reviews: ' ||
    (SELECT json_format(CAST(array_agg(review) AS JSON)) FROM sample_reviews)
) AS executive_summary;
