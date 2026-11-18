-- Gold analytical SQL views over the Silver `apartments` table.
-- Provides deduplicated and aggregated views without modifying the 18-column `apartments` schema.

CREATE OR REPLACE VIEW v_apartments_deduplicated AS
WITH ranked AS (
    SELECT
        a.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                a.city,
                ROUND(a.area_sqm),
                a.floor,
                a.price_eur
            ORDER BY
                (a.total_floors IS NOT NULL) DESC,
                (a.has_balcony IS NOT NULL) DESC,
                a.updated_at DESC NULLS LAST,
                a.id ASC
        ) AS dup_rank
    FROM apartments a
    WHERE a.price_eur IS NOT NULL
      AND a.area_sqm IS NOT NULL
)
SELECT
    id, url, title, transaction_type, category_sub, rooms_count,
    price_eur, price_per_sqm_eur, area_sqm, floor, total_floors,
    property_condition, has_elevator, has_balcony, city, district,
    updated_at, scraped_at
FROM ranked
WHERE dup_rank = 1;

CREATE OR REPLACE VIEW v_district_price_summary AS
SELECT
    district,
    COUNT(*) AS listings_count,
    COUNT(price_eur) AS priced_count,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_eur))::INTEGER AS median_price_eur,
    ROUND(AVG(price_per_sqm_eur), 2) AS avg_price_per_sqm_eur,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY area_sqm)::NUMERIC, 2) AS median_area_sqm
FROM apartments
WHERE district IS NOT NULL
GROUP BY district
ORDER BY listings_count DESC;
