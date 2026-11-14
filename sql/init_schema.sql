-- PostgreSQL schema for Slovak apartment sale listings (Nehnutelnosti.sk)

CREATE TABLE IF NOT EXISTS apartments (
    id VARCHAR(32) PRIMARY KEY,                      -- Unique listing ID (e.g., 'Ju-0LwlGO5z')
    url TEXT NOT NULL,                               -- Canonical detail page URL
    title TEXT,                                      -- Listing headline
    transaction_type VARCHAR(32),                    -- Normalized transaction ('predaj')
    category_sub VARCHAR(64),                        -- Subcategory code (e.g., 'TWO_ROOM_APARTMENT')
    rooms_count SMALLINT,                            -- Resolved room count (1..10)
    price_eur INTEGER,                               -- Total purchase price in EUR
    price_per_sqm_eur NUMERIC(10, 2),                -- Recomputed unit price in EUR/m2
    area_sqm NUMERIC(8, 2),                          -- Usable floor area in m2
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