"""End-to-end PostgreSQL 17 integration tests for Bronze -> Silver -> Gold Medallion ELT.

Runs automatically in GitHub Actions CI against a live `postgres:17` service container
(`RUN_PG_INTEGRATION_TESTS=1`). Skipped locally when `RUN_PG_INTEGRATION_TESTS` is unset.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from src.db import (
    ensure_connection,
    fetch_raw_timestamps,
    get_connection,
    init_db,
    mark_missing_listings_inactive,
    touch_seen_raw_listings,
    upsert_raw_listings_batch,
)
from src.quality_check import run_quality_check
from src.transform import run_transform

RUN_INTEGRATION = os.getenv("RUN_PG_INTEGRATION_TESTS") == "1"


def _make_ad(
    ad_id: str,
    *,
    title: str,
    description: str = "",
    city: str = "Bratislava-Vrakuňa",
    district: str = "okres Bratislava II",
    lat: float = 48.145,
    lon: float = 17.205,
    price_num: int = 169_000,
    unit_price: str = "4 273,07 €/m²",
    area: str = "40",
    floor: int | str = 3,
    total_floors: int = 7,
    sub_cat: str = "TWO_ROOM_APARTMENT",
    state: str = "Kompletná rekonštrukcia",
    has_elevator: Any = True,
    attributes: list[dict[str, str]] | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "id": ad_id,
        "isActive": is_active,
        "title": title,
        "description": description,
        "updatedAt": "2026-11-18T10:00:00+00:00",
        "location": {
            "country": "Slovensko",
            "city": city,
            "district": district,
            "point": {"latitude": lat, "longitude": lon},
        },
        "parameters": {
            "transaction": "Predaj",
            "area": area,
            "floor": floor,
            "numberOfFloors": total_floors,
            "hasElevator": has_elevator,
            "realEstateState": state,
            "construction": "Panelová",
            "category": {"mainValue": "APARTMENTS", "subValue": sub_cat},
            "price": {
                "priceNum": price_num,
                "price": f"{price_num} €",
                "unitPrice": unit_price,
            },
            "attributes": attributes or [],
        },
    }


@unittest.skipUnless(
    RUN_INTEGRATION,
    "Set RUN_PG_INTEGRATION_TESTS=1 with a live PostgreSQL 17 database to run DB integration tests.",
)
class TestMedallionPostgresIntegration(unittest.TestCase):
    """Verify full Bronze -> Silver -> Gold database lifecycle against live PostgreSQL 17."""

    def setUp(self) -> None:
        self.conn = get_connection()
        init_db(self.conn)
        # Verify idempotency on repeated schema/view initialization
        init_db(self.conn)
        with self.conn, self.conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE apartments, raw_listings;")

    def tearDown(self) -> None:
        if not self.conn.closed:
            self.conn.close()

    def test_full_bronze_silver_gold_lifecycle(self) -> None:
        now = datetime.now(UTC)
        older = now - timedelta(days=2)

        bronze_records = [
            # 1. Valid apartment with gross area 40 (exact 39.55 -> interior 36.10 m2 + 3.45 m2 loggia)
            {
                "id": "JuInt001",
                "url": "https://www.nehnutelnosti.sk/detail/JuInt001/2i-byt",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuInt001",
                    title="2-izbový byt s loggiou, Vrakuňa",
                    description="Rozloha: 36,10 m2 + 3,45 m2 loggia.",
                    price_num=169_000,
                    unit_price="4 273,07 €/m²",
                    area="40",
                    floor=3,
                    total_floors=7,
                ),
                "last_seen_at": now,
            },
            # 2. Agency duplicate of JuInt001 (same city, rounded area 36, floor 3, price 169_000)
            {
                "id": "JuInt002",
                "url": "https://www.nehnutelnosti.sk/detail/JuInt002/2i-byt-dup",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuInt002",
                    title="EXKLUZÍVNE 2-izbový byt, Vrakuňa",
                    description="Podlahová plocha bytu je 36,10 m² + 3,45 m² loggia.",
                    price_num=169_000,
                    unit_price="4 273,07 €/m²",
                    area="40",
                    floor=3,
                    total_floors=7,
                ),
                "last_seen_at": now,
            },
            # 3. Reserved listing (kept by default, pruned when exclude_reserved=True)
            {
                "id": "JuIntRes",
                "url": "https://www.nehnutelnosti.sk/detail/JuIntRes/rezervovane",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuIntRes",
                    title="REZERVOVANÉ | 3-izbový byt bez balkóna, Nitra",
                    city="Nitra",
                    district="okres Nitra",
                    lat=48.3069,
                    lon=18.0864,
                    price_num=155_000,
                    unit_price="2 279,41 €/m²",
                    area="68",
                    floor="Prízemie",
                    total_floors=4,
                    sub_cat="THREE_ROOM_APARTMENT",
                    has_elevator="$undefined",
                ),
                "last_seen_at": now,
            },
            # 4. Foreign listing (Croatia GPS): stays cached in Bronze, rejected from Silver
            {
                "id": "JuIntForeign",
                "url": "https://www.nehnutelnosti.sk/detail/JuIntForeign/croatia",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuIntForeign",
                    title="2-izbový apartmán pri mori",
                    lat=43.5147,
                    lon=16.2500,
                ),
                "last_seen_at": now,
            },
            # 5. Stale listing missing from current sitemap: deactivated on full run
            {
                "id": "JuIntStale",
                "url": "https://www.nehnutelnosti.sk/detail/JuIntStale/stale",
                "sitemap_lastmod": older,
                "is_active": True,
                "payload": _make_ad("JuIntStale", title="Starý 1-izbový byt", area="32"),
                "last_seen_at": older,
            },
        ]

        written = upsert_raw_listings_batch(self.conn, bronze_records)
        self.assertEqual(written, 5)

        # Ensure rejected/foreign IDs are tracked in raw_listings timestamps so they aren't re-crawled
        raw_ts = fetch_raw_timestamps(self.conn)
        self.assertIn("JuIntForeign", raw_ts)
        self.assertEqual(len(raw_ts), 5)

        # Touch active unchanged listings and deactivate missing stale ones
        run_cutoff = now - timedelta(minutes=5)
        touched = touch_seen_raw_listings(self.conn, ["JuInt001", "JuInt002"], now)
        self.assertEqual(touched, 2)
        deactivated = mark_missing_listings_inactive(self.conn, run_cutoff)
        self.assertEqual(deactivated, 1)

        # Bronze -> Silver transform (default: keep reserved)
        self.conn = ensure_connection(self.conn)
        stats = run_transform(self.conn, exclude_reserved=False, prune=True)
        self.assertEqual(stats.total_raw, 4)  # 5 total - 1 deactivated
        self.assertEqual(stats.saved_silver, 3)  # JuInt001, JuInt002, JuIntRes (Foreign rejected)
        self.assertEqual(stats.skipped_validation, 1)

        # Verify Silver attributes and Gold views
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT area_sqm, floor, has_balcony, has_elevator, district FROM apartments WHERE id = 'JuInt001';"
            )
            row1 = cur.fetchone()
            assert row1 is not None
            self.assertEqual(float(row1[0]), 36.10)
            self.assertEqual(row1[1], 3)
            self.assertTrue(row1[2])
            self.assertTrue(row1[3])
            self.assertEqual(row1[4], "Bratislava II")

            cur.execute(
                "SELECT floor, has_balcony, has_elevator FROM apartments WHERE id = 'JuIntRes';"
            )
            row_res = cur.fetchone()
            assert row_res is not None
            self.assertEqual(row_res[0], 0)
            self.assertFalse(row_res[1])
            self.assertFalse(row_res[2])

            # Gold deduplication collapses JuInt001 & JuInt002 into 1 row (+ JuIntRes = 2)
            cur.execute("SELECT COUNT(*) FROM v_apartments_deduplicated;")
            self.assertEqual(cur.fetchone()[0], 2)

            cur.execute("SELECT COUNT(*) FROM v_district_price_summary;")
            self.assertEqual(cur.fetchone()[0], 2)

        # All 6 automated data quality invariants must report 0 violations
        self.assertEqual(run_quality_check(self.conn), 0)

        # Offline re-transform with --exclude-reserved must prune JuIntRes cleanly
        stats_no_res = run_transform(self.conn, exclude_reserved=True, prune=True)
        self.assertEqual(stats_no_res.saved_silver, 2)
        self.assertEqual(stats_no_res.pruned_silver, 1)


if __name__ == "__main__":
    unittest.main()
