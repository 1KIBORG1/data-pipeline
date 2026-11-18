-- PostgreSQL 17 schema for Nehnutelnosti.sk apartment listings.
-- Bronze: `raw_listings` (stripped RSC payload + crawl timestamps)
-- Silver: `apartments` (validated 18-column table)

CREATE TABLE IF NOT EXISTS raw_listings (
    id VARCHAR(32) PRIMARY KEY,                      -- Listing ID from URL (e.g. 'Ju...')
    url TEXT NOT NULL,                               -- Detail page URL
    sitemap_lastmod TIMESTAMPTZ,                     -- Last modification timestamp from XML sitemap
    is_active BOOLEAN NOT NULL DEFAULT TRUE,         -- FALSE once removed from sitemap or marked inactive
    payload JSONB NOT NULL,                          -- Stripped Next.js RSC advertisement JSON
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),-- First discovery timestamp
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- Most recent sitemap sighting timestamp
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()    -- Most recent detail-page download timestamp
);

CREATE INDEX IF NOT EXISTS idx_raw_listings_active
    ON raw_listings (is_active);

CREATE INDEX IF NOT EXISTS idx_raw_listings_last_seen
    ON raw_listings (last_seen_at);

CREATE TABLE IF NOT EXISTS apartments (
    id VARCHAR(32) PRIMARY KEY,                      -- Listing ID from URL (e.g. 'Ju...')
    url TEXT NOT NULL,                               -- Canonical detail page URL
    title TEXT,                                      -- Listing headline
    transaction_type VARCHAR(32),                    -- Normalized transaction ('predaj')
    category_sub VARCHAR(64),                        -- Subcategory code (e.g., 'TWO_ROOM_APARTMENT')
    rooms_count SMALLINT,                            -- Resolved room count (1..10)
    price_eur INTEGER,                               -- Total purchase price in EUR
    price_per_sqm_eur NUMERIC(10, 2),                -- Recomputed unit price in EUR/m2
    area_sqm NUMERIC(8, 2),                          -- Pure interior floor area in m2
    floor SMALLINT,                                  -- Apartment floor (-2..46, 0 = ground floor)
    total_floors SMALLINT,                           -- Total building floors (> 1)
    property_condition VARCHAR(64),                  -- Building state (e.g., 'Novostavba')
    has_elevator BOOLEAN NOT NULL DEFAULT FALSE,     -- Elevator availability
    has_balcony BOOLEAN,                             -- Three-valued balcony/loggia/terrace (TRUE, FALSE, or NULL when unspecified)
    city VARCHAR(128),                               -- Municipality / city part
    district VARCHAR(128),                           -- District name without 'okres ' prefix
    updated_at TIMESTAMPTZ,                          -- Source listing update timestamp
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()    -- Pipeline upsert timestamp
);

CREATE INDEX IF NOT EXISTS idx_apartments_district_city
    ON apartments (district, city);

CREATE INDEX IF NOT EXISTS idx_apartments_updated_at
    ON apartments (updated_at);

CREATE INDEX IF NOT EXISTS idx_apartments_price_area
    ON apartments (price_eur, area_sqm);
