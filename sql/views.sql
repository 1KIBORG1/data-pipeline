-- Gold analytical SQL views over Silver `apartments` and Bronze `raw_listings`.
-- Preserves the 18-column `apartments` schema while providing deduplicated,
-- district-aggregated, and Bronze-enriched views for downstream modeling.

CREATE OR REPLACE VIEW v_apartments_deduplicated AS
WITH ranked AS (
    SELECT
        a.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                CASE
                    WHEN jsonb_typeof(r.payload->'devProject') = 'object' THEN 'dev:' || a.id
                    ELSE 'resale'
                END,
                a.city,
                a.rooms_count,
                ROUND(a.area_sqm),
                COALESCE(a.floor::TEXT, 'null:' || a.id),
                a.price_eur
            ORDER BY
                (a.total_floors IS NOT NULL) DESC,
                (a.has_balcony IS NOT NULL) DESC,
                a.updated_at DESC NULLS LAST,
                a.id ASC
        ) AS dup_rank
    FROM apartments a
    LEFT JOIN raw_listings r ON r.id = a.id
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
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY area_sqm)::NUMERIC, 2) AS median_area_sqm,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_per_sqm_eur)::NUMERIC, 2) AS median_price_per_sqm_eur
FROM v_apartments_deduplicated
WHERE district IS NOT NULL
GROUP BY district
ORDER BY listings_count DESC;

CREATE OR REPLACE VIEW v_apartments_enriched AS
SELECT
    a.id,
    a.url,
    a.title,
    a.transaction_type,
    a.category_sub,
    a.rooms_count,
    a.price_eur,
    a.price_per_sqm_eur,
    a.area_sqm,
    a.floor,
    a.total_floors,
    a.property_condition,
    a.has_elevator,
    a.has_balcony,
    a.city,
    a.district,
    NULLIF(NULLIF(BTRIM(r.payload->'location'->>'street'), ''), '$undefined') AS street,
    CASE
        WHEN (r.payload->'location'->'point'->>'latitude') ~ '^-?[0-9]+(\.[0-9]+)?$'
        THEN (r.payload->'location'->'point'->>'latitude')::DOUBLE PRECISION
    END AS latitude,
    CASE
        WHEN (r.payload->'location'->'point'->>'longitude') ~ '^-?[0-9]+(\.[0-9]+)?$'
        THEN (r.payload->'location'->'point'->>'longitude')::DOUBLE PRECISION
    END AS longitude,
    NULLIF(NULLIF(BTRIM(r.payload->'parameters'->>'construction'), ''), '$undefined') AS construction,
    CASE
        WHEN (r.payload->'parameters'->>'yearOfConstruction') ~ '^(1[89][0-9]{2}|20[0-4][0-9])$'
        THEN (r.payload->'parameters'->>'yearOfConstruction')::SMALLINT
    END AS year_of_construction,
    COALESCE(jsonb_typeof(r.payload->'devProject') = 'object', FALSE) AS is_developer_project,
    r.first_seen_at,
    r.last_seen_at,
    a.updated_at,
    a.scraped_at
FROM apartments a
LEFT JOIN raw_listings r ON r.id = a.id;
