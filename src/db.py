"""PostgreSQL persistence for Bronze (`raw_listings`) and Silver (`apartments`) tables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from typing import Any

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extras import Json, execute_values

from src.config import SQL_DIR, DatabaseConfig

APARTMENT_COLUMNS: tuple[str, ...] = (
    "id",
    "url",
    "title",
    "transaction_type",
    "category_sub",
    "rooms_count",
    "price_eur",
    "price_per_sqm_eur",
    "area_sqm",
    "floor",
    "total_floors",
    "property_condition",
    "has_elevator",
    "has_balcony",
    "city",
    "district",
    "updated_at",
)

UPSERT_RAW_LISTINGS_SQL = """
INSERT INTO raw_listings (
    id, url, sitemap_lastmod, is_active, payload, last_seen_at, scraped_at
)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    url = EXCLUDED.url,
    sitemap_lastmod = COALESCE(EXCLUDED.sitemap_lastmod, raw_listings.sitemap_lastmod),
    is_active = EXCLUDED.is_active,
    payload = EXCLUDED.payload,
    last_seen_at = EXCLUDED.last_seen_at,
    scraped_at = NOW();
"""

UPSERT_APARTMENTS_BATCH_SQL = """
INSERT INTO apartments (
    id, url, title, transaction_type, category_sub, rooms_count,
    price_eur, price_per_sqm_eur, area_sqm, floor, total_floors,
    property_condition, has_elevator, has_balcony, city, district,
    updated_at
)
VALUES %s
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


def get_connection(cfg: DatabaseConfig | None = None) -> Connection:
    """Create and return a new PostgreSQL connection configured via environment variables."""
    db_cfg = cfg or DatabaseConfig.from_env()
    return psycopg2.connect(
        host=db_cfg.host,
        port=db_cfg.port,
        dbname=db_cfg.dbname,
        user=db_cfg.user,
        password=db_cfg.password,
        sslmode=db_cfg.sslmode,
        connect_timeout=db_cfg.connect_timeout,
    )


def ensure_connection(conn: Connection | None, cfg: DatabaseConfig | None = None) -> Connection:
    """Return an active PostgreSQL connection, transparently reconnecting if closed or broken."""
    if conn is None or conn.closed != 0:
        return get_connection(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except psycopg2.Error:
        with suppress(Exception):
            conn.close()
        return get_connection(cfg)


def init_db(conn: Connection) -> None:
    """Ensure Bronze (`raw_listings`), Silver (`apartments`), indexes, and Gold views exist."""
    schema_sql = (SQL_DIR / "init_schema.sql").read_text(encoding="utf-8")
    views_sql = (SQL_DIR / "views.sql").read_text(encoding="utf-8")

    with conn, conn.cursor() as cur:
        cur.execute(schema_sql)
        # Only migrate legacy NOT NULL constraint on `has_balcony` if still present
        cur.execute(
            """
            SELECT is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'apartments'
              AND column_name = 'has_balcony';
            """
        )
        col_meta = cur.fetchone()
        if col_meta is not None and (col_meta[0] == "NO" or col_meta[1] is not None):
            cur.execute(
                "ALTER TABLE apartments ALTER COLUMN has_balcony DROP NOT NULL; "
                "ALTER TABLE apartments ALTER COLUMN has_balcony DROP DEFAULT;"
            )
        cur.execute(views_sql)


def fetch_raw_timestamps(conn: Connection) -> dict[str, datetime | None]:
    """Load `{id: sitemap_lastmod}` from `raw_listings` (including rejected/inactive IDs).

    Using `raw_listings` rather than only `apartments` guarantees that previously rejected
    listings (houses, rentals, buyer-wanted ads, foreign resorts) are NOT re-downloaded
    on every subsequent incremental crawl unless their `<lastmod>` timestamp changes.
    """
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, COALESCE(sitemap_lastmod, scraped_at) FROM raw_listings")
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_existing_timestamps(conn: Connection) -> dict[str, datetime | None]:
    """Load `{id: updated_at}` mapping from Silver `apartments` (legacy helper)."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, updated_at FROM apartments")
        return {row[0]: row[1] for row in cur.fetchall()}


def upsert_raw_listings_batch(conn: Connection, records: list[dict[str, Any]]) -> int:
    """Batch-upsert raw Next.js RSC advertisement payloads into Bronze `raw_listings`."""
    if not records:
        return 0

    values = [
        (
            rec["id"],
            rec["url"],
            rec.get("sitemap_lastmod"),
            bool(rec.get("is_active", True)),
            Json(rec["payload"]),
            rec["last_seen_at"],
            rec.get("scraped_at") or rec["last_seen_at"],
        )
        for rec in records
    ]
    with conn, conn.cursor() as cur:
        execute_values(cur, UPSERT_RAW_LISTINGS_SQL, values, page_size=200)
    return len(values)


def touch_seen_raw_listings(
    conn: Connection,
    seen_ids: list[str],
    run_started_at: datetime,
) -> int:
    """Refresh `last_seen_at` and `is_active = TRUE` for unchanged listings present in the sitemap."""
    if not seen_ids:
        return 0

    total_updated = 0
    chunk_size = 1000
    with conn, conn.cursor() as cur:
        for start in range(0, len(seen_ids), chunk_size):
            chunk = seen_ids[start : start + chunk_size]
            cur.execute(
                """
                UPDATE raw_listings
                SET last_seen_at = %s,
                    is_active = TRUE
                WHERE id = ANY(%s)
                  AND (payload->>'isActive' IS DISTINCT FROM 'false');
                """,
                (run_started_at, chunk),
            )
            total_updated += cur.rowcount
    return total_updated


def mark_missing_listings_inactive(conn: Connection, run_started_at: datetime) -> int:
    """Mark Bronze listings absent from the latest full sitemap as `is_active = FALSE`."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw_listings
            SET is_active = FALSE
            WHERE is_active = TRUE
              AND last_seen_at < %s;
            """,
            (run_started_at,),
        )
        return cur.rowcount


def iter_active_raw_listings(
    conn: Connection,
    batch_size: int = 1000,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Stream `(id, url, payload)` tuples for all active Bronze listings using keyset pagination."""
    last_id = ""
    while True:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, url, payload
                FROM raw_listings
                WHERE is_active = TRUE
                  AND id > %s
                ORDER BY id ASC
                LIMIT %s;
                """,
                (last_id, batch_size),
            )
            rows = cur.fetchall()
        if not rows:
            break
        for row in rows:
            yield row[0], row[1], row[2]
        last_id = rows[-1][0]


def upsert_apartments_batch(conn: Connection, rows: list[dict[str, Any]]) -> int:
    """Batch-upsert validated apartment records into the Silver `apartments` table."""
    if not rows:
        return 0

    values = [tuple(row.get(col) for col in APARTMENT_COLUMNS) for row in rows]
    with conn, conn.cursor() as cur:
        execute_values(cur, UPSERT_APARTMENTS_BATCH_SQL, values, page_size=200)
    return len(values)


def upsert_apartment(conn: Connection, apt_data: dict[str, Any]) -> None:
    """Upsert a single validated apartment record into `apartments`."""
    upsert_apartments_batch(conn, [apt_data])


def prune_apartments_not_in(conn: Connection, valid_ids: set[str]) -> int:
    """Remove Silver `apartments` rows that are no longer active or no longer pass validation."""
    with conn, conn.cursor() as cur:
        if not valid_ids:
            cur.execute("DELETE FROM apartments;")
            return cur.rowcount
        cur.execute(
            "DELETE FROM apartments WHERE NOT (id = ANY(%s));",
            (list(valid_ids),),
        )
        return cur.rowcount
