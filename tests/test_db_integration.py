"""End-to-end PostgreSQL 17 integration tests for Bronze -> Silver -> Gold ELT.

Runs automatically in GitHub Actions CI against a live `postgres:17` service container
(`RUN_PG_INTEGRATION_TESTS=1`). Skipped locally when `RUN_PG_INTEGRATION_TESTS` is unset.
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from src.db import (
    ensure_connection,
    fetch_raw_timestamps,
    get_connection,
    init_db,
    mark_ids_inactive,
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
    street: str = "Stavbárska",
    lat: float = 48.145,
    lon: float = 17.205,
    price_num: int = 169_000,
    unit_price: str = "4 273,07 €/m²",
    area: str = "40",
    floor: int | str | None = 3,
    total_floors: int | None = 7,
    sub_cat: str = "TWO_ROOM_APARTMENT",
    state: str = "Kompletná rekonštrukcia",
    construction: str = "Panelová",
    year_of_construction: int | None = 1984,
    has_elevator: Any = True,
    dev_project: dict[str, Any] | None = None,
    attributes: list[dict[str, str]] | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ad_id,
        "isActive": is_active,
        "title": title,
        "description": description,
        "updatedAt": "2026-11-18T10:00:00+00:00",
        "location": {
            "country": "Slovensko",
            "city": city,
            "district": district,
            "street": street,
            "point": {"latitude": lat, "longitude": lon},
        },
        "parameters": {
            "transaction": "Predaj",
            "area": area,
            "floor": floor,
            "numberOfFloors": total_floors,
            "hasElevator": has_elevator,
            "realEstateState": state,
            "construction": construction,
            "yearOfConstruction": year_of_construction,
            "category": {"mainValue": "APARTMENTS", "subValue": sub_cat},
            "price": {
                "priceNum": price_num,
                "price": f"{price_num} €",
                "unitPrice": unit_price,
            },
            "attributes": attributes or [],
        },
    }
    if dev_project is not None:
        payload["devProject"] = dev_project
    return payload


@unittest.skipUnless(
    RUN_INTEGRATION,
    "Set RUN_PG_INTEGRATION_TESTS=1 with a live PostgreSQL 17 database to run DB integration tests.",
)
class TestMedallionPostgresIntegration(unittest.TestCase):
    """Verify full Bronze -> Silver -> Gold database lifecycle against live PostgreSQL 17."""

    def setUp(self) -> None:
        self.conn = get_connection()
        init_db(self.conn)
        init_db(self.conn)
        with self.conn, self.conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE apartments, raw_listings;")

    def tearDown(self) -> None:
        if not self.conn.closed:
            self.conn.close()

    def test_full_bronze_silver_gold_lifecycle(self) -> None:
        now = datetime.now(UTC)
        older = now - timedelta(days=2)

        # Verify ensure_connection does not leave connection idle-in-transaction
        self.conn = ensure_connection(self.conn)
        self.assertEqual(self.conn.info.transaction_status, TRANSACTION_STATUS_IDLE)

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
            # 2. Agency duplicate of JuInt001 (same city, rooms, rounded area 36, floor 3, price 169_000)
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
            # 3 & 4. Two distinct developer-project units with identical floor/area/price (must NOT merge)
            {
                "id": "JuDevA",
                "url": "https://www.nehnutelnosti.sk/detail/JuDevA/novostavba-a",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuDevA",
                    title="2-izbový byt B.401 v novostavbe",
                    city="Košice-Západ",
                    district="okres Košice II",
                    lat=48.7164,
                    lon=21.2350,
                    price_num=210_000,
                    unit_price="4 038,46 €/m²",
                    area="52",
                    floor=4,
                    total_floors=8,
                    state="Novostavba",
                    construction="Tehlová",
                    year_of_construction=2026,
                    dev_project={"id": "proj-1", "name": "Rezidencia X"},
                ),
                "last_seen_at": now,
            },
            {
                "id": "JuDevB",
                "url": "https://www.nehnutelnosti.sk/detail/JuDevB/novostavba-b",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuDevB",
                    title="2-izbový byt B.402 v novostavbe",
                    city="Košice-Západ",
                    district="okres Košice II",
                    lat=48.7164,
                    lon=21.2350,
                    price_num=210_000,
                    unit_price="4 038,46 €/m²",
                    area="52",
                    floor=4,
                    total_floors=8,
                    state="Novostavba",
                    construction="Tehlová",
                    year_of_construction=2026,
                    dev_project={"id": "proj-1", "name": "Rezidencia X"},
                ),
                "last_seen_at": now,
            },
            # 5 & 6. Two listings in the same city with NULL floor (must NOT merge on NULL=NULL)
            {
                "id": "JuNullFl1",
                "url": "https://www.nehnutelnosti.sk/detail/JuNullFl1/byt-1",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuNullFl1",
                    title="3-izbový byt v centre, Žilina",
                    city="Žilina",
                    district="okres Žilina",
                    lat=49.2231,
                    lon=18.7394,
                    price_num=180_000,
                    unit_price="3 000,00 €/m²",
                    area="60",
                    floor=None,
                    total_floors=None,
                    sub_cat="THREE_ROOM_APARTMENT",
                ),
                "last_seen_at": now,
            },
            {
                "id": "JuNullFl2",
                "url": "https://www.nehnutelnosti.sk/detail/JuNullFl2/byt-2",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad(
                    "JuNullFl2",
                    title="3-izbový byt Vlčince, Žilina",
                    city="Žilina",
                    district="okres Žilina",
                    lat=49.2100,
                    lon=18.7600,
                    price_num=180_000,
                    unit_price="3 000,00 €/m²",
                    area="60",
                    floor=None,
                    total_floors=None,
                    sub_cat="THREE_ROOM_APARTMENT",
                ),
                "last_seen_at": now,
            },
            # 7. Reserved listing (kept by default, pruned when exclude_reserved=True)
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
            # 8. Foreign listing (Croatia GPS): stays cached in Bronze, rejected from Silver
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
            # 9. Confirmed expired listing (explicitly deactivated via mark_ids_inactive)
            {
                "id": "JuExpiredBanner",
                "url": "https://www.nehnutelnosti.sk/detail/JuExpiredBanner/exp",
                "sitemap_lastmod": now,
                "is_active": True,
                "payload": _make_ad("JuExpiredBanner", title="1-izbový byt", area="30"),
                "last_seen_at": now,
            },
            # 10. Stale listing missing from current sitemap: deactivated on full run
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
        self.assertEqual(written, 10)

        raw_ts = fetch_raw_timestamps(self.conn)
        self.assertIn("JuIntForeign", raw_ts)
        self.assertEqual(len(raw_ts), 10)

        # Explicitly deactivate banner-expired ID, touch seen IDs, deactivate missing stale IDs
        self.assertEqual(mark_ids_inactive(self.conn, ["JuExpiredBanner"]), 1)
        run_cutoff = now - timedelta(minutes=5)
        touched = touch_seen_raw_listings(self.conn, ["JuInt001", "JuInt002"], now)
        self.assertEqual(touched, 2)
        deactivated = mark_missing_listings_inactive(self.conn, run_cutoff)
        self.assertEqual(deactivated, 1)

        # Bronze -> Silver transform (8 active Bronze: 7 valid Silver + 1 foreign rejected)
        stats = run_transform(self.conn, exclude_reserved=False, prune=True)
        self.assertEqual(stats.total_raw, 8)
        self.assertEqual(stats.saved_silver, 7)
        self.assertEqual(stats.skipped_validation, 1)

        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "SELECT area_sqm, floor, has_balcony, has_elevator, district, scraped_at "
                "FROM apartments WHERE id = 'JuInt001';"
            )
            row1 = cur.fetchone()
            assert row1 is not None
            self.assertEqual(float(row1[0]), 36.10)
            self.assertEqual(row1[1], 3)
            self.assertTrue(row1[2])
            self.assertTrue(row1[3])
            self.assertEqual(row1[4], "Bratislava II")
            first_scraped_at = row1[5]

            # Gold deduplication:
            # JuInt001 + JuInt002 -> 1 row
            # JuDevA + JuDevB -> 2 rows (devProject preserved)
            # JuNullFl1 + JuNullFl2 -> 2 rows (NULL floor not merged)
            # JuIntRes -> 1 row
            # Total deduplicated = 6 rows across 4 districts
            cur.execute("SELECT COUNT(*) FROM v_apartments_deduplicated;")
            self.assertEqual(cur.fetchone()[0], 6)

            cur.execute(
                "SELECT listings_count, median_price_per_sqm_eur "
                "FROM v_district_price_summary WHERE district = 'Bratislava II';"
            )
            dist_row = cur.fetchone()
            assert dist_row is not None
            self.assertEqual(dist_row[0], 1)  # Deduplicated count (not 2)
            self.assertIsNotNone(dist_row[1])

            # Verify v_apartments_enriched exposes Bronze spatial & building metadata
            cur.execute(
                "SELECT street, latitude, longitude, construction, year_of_construction, is_developer_project "
                "FROM v_apartments_enriched WHERE id = 'JuDevA';"
            )
            enr = cur.fetchone()
            assert enr is not None
            self.assertEqual(enr[0], "Stavbárska")
            self.assertAlmostEqual(float(enr[1]), 48.7164, places=4)
            self.assertAlmostEqual(float(enr[2]), 21.2350, places=4)
            self.assertEqual(enr[3], "Tehlová")
            self.assertEqual(enr[4], 2026)
            self.assertTrue(enr[5])

        self.assertEqual(run_quality_check(self.conn), 0)

        # Verify IS DISTINCT FROM upsert does NOT bump scraped_at on unchanged rows
        time.sleep(0.05)
        stats_no_res = run_transform(self.conn, exclude_reserved=True, prune=True)
        self.assertEqual(stats_no_res.saved_silver, 6)
        self.assertEqual(stats_no_res.pruned_silver, 1)

        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT scraped_at FROM apartments WHERE id = 'JuInt001';")
            second_scraped_at = cur.fetchone()[0]
            self.assertEqual(first_scraped_at, second_scraped_at)


if __name__ == "__main__":
    unittest.main()
