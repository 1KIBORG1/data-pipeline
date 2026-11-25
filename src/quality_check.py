"""PostgreSQL data quality check CLI for Bronze, Silver, and Gold layers.

Exits with status code 0 when all SQL invariants pass, or 1 on any violation.
"""

from __future__ import annotations

import sys
from typing import Any

from psycopg2.extensions import connection as Connection

from src.db import get_connection, init_db


def _scalar(conn: Connection, query: str) -> Any:
    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
        return row[0] if row else 0


def run_quality_check(conn: Connection) -> int:
    """Print Bronze/Silver/Gold health metrics and return count of hard invariant violations."""
    bronze_total = _scalar(conn, "SELECT COUNT(*) FROM raw_listings;")
    bronze_active = _scalar(conn, "SELECT COUNT(*) FROM raw_listings WHERE is_active = TRUE;")
    bronze_inactive = bronze_total - bronze_active
    silver_total = _scalar(conn, "SELECT COUNT(*) FROM apartments;")
    silver_priced_area = _scalar(
        conn,
        "SELECT COUNT(*) FROM apartments WHERE price_eur IS NOT NULL AND area_sqm IS NOT NULL;",
    )
    gold_unique = _scalar(conn, "SELECT COUNT(*) FROM v_apartments_deduplicated;")
    dup_rows = max(silver_priced_area - gold_unique, 0)

    print("=" * 72)
    print("Nehnutelnosti.sk Data Quality Report")
    print("=" * 72)
    print(f"Bronze `raw_listings`       : {bronze_total:>7,} (active: {bronze_active:,}, inactive: {bronze_inactive:,})")
    print(f"Silver `apartments`         : {silver_total:>7,} (with price & area: {silver_priced_area:,})")
    print(f"Gold   `v_apartments_dedup` : {gold_unique:>7,} (estimated agency duplicates: {dup_rows:,})")

    if silver_total == 0:
        print("\nSilver `apartments` table is currently empty.")
        return 0

    print("\n--- Column Completeness (Silver `apartments`) ---")
    cols = [
        "price_eur",
        "area_sqm",
        "price_per_sqm_eur",
        "rooms_count",
        "floor",
        "total_floors",
        "property_condition",
        "has_balcony",
        "city",
        "district",
        "updated_at",
    ]
    select_parts = ", ".join(f"COUNT({c})" for c in cols)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {select_parts} FROM apartments;")
        counts = cur.fetchone() or ()
    for col, non_null in zip(cols, counts, strict=True):
        pct = (non_null / silver_total) * 100.0
        print(f"  {col:<20s} : {non_null:>7,} / {silver_total:,} ({pct:5.1f}%)")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE has_balcony IS TRUE)  AS balc_true,
                COUNT(*) FILTER (WHERE has_balcony IS FALSE) AS balc_false,
                COUNT(*) FILTER (WHERE has_balcony IS NULL)  AS balc_null
            FROM apartments;
            """
        )
        b_true, b_false, b_null = cur.fetchone() or (0, 0, 0)
    print(
        f"\nThree-valued `has_balcony`  : TRUE={b_true:,} ({b_true/silver_total*100:.1f}%), "
        f"FALSE={b_false:,} ({b_false/silver_total*100:.1f}%), "
        f"NULL={b_null:,} ({b_null/silver_total*100:.1f}%)"
    )

    print("\n--- Hard Data Quality Invariants (expected: 0) ---")
    invariant_queries: list[tuple[str, str]] = [
        (
            "price_per_sqm formula drift (> 0.05 EUR)",
            """
            SELECT COUNT(*) FROM apartments
            WHERE price_eur IS NOT NULL AND area_sqm IS NOT NULL AND area_sqm > 0
              AND (
                  price_per_sqm_eur IS NULL
                  OR ABS(price_per_sqm_eur - ROUND(price_eur::numeric / area_sqm, 2)) > 0.05
              );
            """,
        ),
        (
            "floor > total_floors",
            """
            SELECT COUNT(*) FROM apartments
            WHERE floor IS NOT NULL AND total_floors IS NOT NULL AND floor > total_floors;
            """,
        ),
        (
            "total_floors <= 1",
            "SELECT COUNT(*) FROM apartments WHERE total_floors IS NOT NULL AND total_floors <= 1;",
        ),
        (
            "sale price_per_sqm out of [300, 25000]",
            """
            SELECT COUNT(*) FROM apartments
            WHERE COALESCE(transaction_type, 'predaj') <> 'prenajom'
              AND price_per_sqm_eur IS NOT NULL
              AND (price_per_sqm_eur < 300 OR price_per_sqm_eur > 25000);
            """,
        ),
        (
            "area_sqm out of [10, 600]",
            """
            SELECT COUNT(*) FROM apartments
            WHERE area_sqm IS NOT NULL AND (area_sqm < 10 OR area_sqm > 600);
            """,
        ),
        (
            "Panelová building labeled Novostavba (Bronze cross-check)",
            """
            SELECT COUNT(*)
            FROM apartments a
            JOIN raw_listings r ON r.id = a.id
            WHERE a.property_condition = 'Novostavba'
              AND r.payload->'parameters'->>'construction' ILIKE 'panelov%';
            """,
        ),
    ]

    total_violations = 0
    for label, sql in invariant_queries:
        cnt = int(_scalar(conn, sql))
        status = "OK" if cnt == 0 else "FAIL"
        total_violations += cnt
        print(f"  [{status:4s}] {label:<58s}: {cnt}")

    print("\n--- Top 10 Districts (Gold `v_district_price_summary`) ---")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT district, listings_count, median_price_eur, median_price_per_sqm_eur, median_area_sqm
            FROM v_district_price_summary
            LIMIT 10;
            """
        )
        rows = cur.fetchall()
    print(f"  {'District':<24s} {'Count':>7s} {'Median EUR':>12s} {'Med EUR/m2':>10s} {'Area m2':>8s}")
    for dist, cnt, med_p, med_pps, med_a in rows:
        p_str = f"{med_p:,.0f}" if med_p is not None else "--"
        pps_str = f"{med_pps:,.0f}" if med_pps is not None else "--"
        a_str = f"{med_a:.1f}" if med_a is not None else "--"
        print(f"  {str(dist):<24s} {cnt:>7,d} {p_str:>12s} {pps_str:>10s} {a_str:>8s}")

    print("=" * 72)
    return total_violations


def main() -> int:
    conn = get_connection()
    try:
        init_db(conn)
        violations = run_quality_check(conn)
    finally:
        if not conn.closed:
            conn.close()
    return 1 if violations > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
