import os
from datetime import datetime
from typing import Any

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as Connection

load_dotenv()

CREATE_APARTMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS apartments (
    id VARCHAR(32) PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    transaction_type VARCHAR(32),
    category_sub VARCHAR(64),
    rooms_count SMALLINT,
    price_eur INTEGER,
    price_per_sqm_eur NUMERIC(10, 2),
    area_sqm NUMERIC(8, 2),
    floor SMALLINT,
    total_floors SMALLINT,
    property_condition VARCHAR(64),
    has_elevator BOOLEAN NOT NULL DEFAULT FALSE,
    has_balcony BOOLEAN NOT NULL DEFAULT FALSE,
    city VARCHAR(128),
    district VARCHAR(128),
    updated_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

UPSERT_APARTMENT_SQL = """
INSERT INTO apartments (
    id, url, title, transaction_type, category_sub, rooms_count,
    price_eur, price_per_sqm_eur, area_sqm, floor, total_floors,
    property_condition, has_elevator, has_balcony, city, district,
    updated_at
)
VALUES (
    %(id)s, %(url)s, %(title)s, %(transaction_type)s, %(category_sub)s, %(rooms_count)s,
    %(price_eur)s, %(price_per_sqm_eur)s, %(area_sqm)s, %(floor)s, %(total_floors)s,
    %(property_condition)s, %(has_elevator)s, %(has_balcony)s, %(city)s, %(district)s,
    %(updated_at)s
)
ON CONFLICT (id) DO UPDATE SET
    url = EXCLUDED.url,
    title = EXCLUDED.title,
    transaction_type = EXCLUDED.transaction_type,
    category_sub = EXCLUDED.category_sub,
    rooms_count = EXCLUDED.rooms_count,
    price_eur = EXCLUDED.price_eur,
    price_per_sqm_eur = EXCLUDED.price_per_sqm_eur,
    area_sqm = EXCLUDED.area_sqm,
    floor = EXCLUDED.floor,
    total_floors = EXCLUDED.total_floors,
    property_condition = EXCLUDED.property_condition,
    has_elevator = EXCLUDED.has_elevator,
    has_balcony = EXCLUDED.has_balcony,
    city = EXCLUDED.city,
    district = EXCLUDED.district,
    updated_at = EXCLUDED.updated_at,
    scraped_at = NOW();
"""


def get_connection() -> Connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "15432")),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode=os.getenv("DB_SSLMODE", "disable"),
        connect_timeout=10
    )


def init_db(conn: Connection) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_APARTMENTS_TABLE_SQL)


def upsert_apartment(conn: Connection, apt_data: dict[str, Any]) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(UPSERT_APARTMENT_SQL, apt_data)


def fetch_existing_timestamps(conn: Connection) -> dict[str, datetime | None]:
    with conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, updated_at FROM apartments')
            return {row[0]: row[1] for row in cur.fetchall()}


