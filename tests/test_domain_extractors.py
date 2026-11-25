"""Unit tests for domain extractors, section splitting, GPS bounds, and raw adapter."""

import unittest
from datetime import UTC, datetime

from pydantic import ValidationError
from scrapy.http import HtmlResponse

from src.domain.area import compute_exact_portal_area, fix_missing_decimal_area
from src.domain.reference import is_within_bratislava_gps, is_within_slovakia_gps
from src.domain.sections import split_description_sections
from src.models import ApartmentModel
from src.parser import parse_apartment_desc_dict
from src.spider import ApartmentSitemapSpider, _parse_utc_timestamp, extract_ad_id_from_url


class TestExactAreaAndDecimalCorrection(unittest.TestCase):
    """Verify exact portal area recovery (`priceNum / unitPrice`) and safe `/10` typo repair."""

    def test_exact_portal_area_recovers_decimals(self) -> None:
        self.assertEqual(compute_exact_portal_area(40.0, 84_900, "2\xa0146,65 €/m²"), 39.55)
        self.assertEqual(compute_exact_portal_area(51.0, 169_999, "3 341,18 €/m²"), 50.88)
        # Reject stale unitPrice when implied area differs by > 0.55 m² from portal area
        self.assertEqual(compute_exact_portal_area(61.0, 210_000, "3 000,00 €/m²"), 61.0)

    def test_missing_decimal_fixes_654_but_preserves_luxury_loft(self) -> None:
        # 654 m² on a 2-room apartment at 195k EUR -> 65.4 m² (runs BEFORE 600 m² cutoff!)
        self.assertEqual(fix_missing_decimal_area(654.0, rooms_count=2, price_eur=195_000, min_area=20.0), 65.4)
        # Genuine 215 m² luxury 2-room penthouse at 950k EUR (4,418 €/m²) must NOT be divided by 10
        self.assertEqual(fix_missing_decimal_area(215.0, rooms_count=2, price_eur=950_000, min_area=20.0), 215.0)

        apt_654 = ApartmentModel(
            id="JuTypo654",
            url="https://www.nehnutelnosti.sk/detail/JuTypo654/x",
            title="2-izbový byt, Trenčín",
            category_sub="TWO_ROOM_APARTMENT",
            area_sqm=654.0,
            price_eur=195_000,
            city="Trenčín",
            district="Trenčín",
        )
        self.assertEqual(apt_654.area_sqm, 65.4)

        apt_loft = ApartmentModel(
            id="JuLuxury215",
            url="https://www.nehnutelnosti.sk/detail/JuLuxury215/x",
            title="Luxusný 2-izbový loft, Eurovea",
            category_sub="TWO_ROOM_APARTMENT",
            area_sqm=215.0,
            price_eur=950_000,
            city="Bratislava-Staré Mesto",
            district="Bratislava I",
        )
        self.assertEqual(apt_loft.area_sqm, 215.0)


class TestSectionSplitterAndDoubleNegation(unittest.TestCase):
    """Verify Slovak description section isolation and balcony/elevator edge cases."""

    def test_section_splitter_isolates_lokalita_but_keeps_bullet_specs(self) -> None:
        desc = (
            "ZÁKLADNÉ ÚDAJE:\n"
            "* Lokalita: Rajčianska ul., Bratislava – Vrakuňa\n"
            "* Poschodie: 5/8\n"
            "* Výmera: 51 m2\n\n"
            "LOKALITA A OKOLIE:\n"
            "V blízkosti je OC Terasa, kaviarne s letnou terasou a reštaurácie.\n"
        )
        sec = split_description_sections(desc)
        self.assertIn("Poschodie: 5/8", sec.non_location_text)
        self.assertNotIn("OC Terasa", sec.non_location_text)
        self.assertIn("OC Terasa", sec.location_text)

    def test_double_negation_balcony_with_terrace_resolves_true(self) -> None:
        apt = ApartmentModel(
            id="JuBalcTerrace",
            url="https://www.nehnutelnosti.sk/detail/JuBalcTerrace/x",
            title="2-izbový byt v novostavbe",
            description="Byt nemá balkón, ale má priestrannú terasu 18 m².",
            category_sub="TWO_ROOM_APARTMENT",
            city="Nitra",
            district="Nitra",
        )
        self.assertTrue(apt.has_balcony)

    def test_planned_future_balcony_does_not_mark_true(self) -> None:
        apt = ApartmentModel(
            id="JuPlannedBalc",
            url="https://www.nehnutelnosti.sk/detail/JuPlannedBalc/x",
            title="2-izbový byt v zateplenom dome",
            description="K bytu bude prislúchať loggia po plánovanej dostavbe balkónov v roku 2028.",
            category_sub="TWO_ROOM_APARTMENT",
            city="Trnava",
            district="Trnava",
        )
        self.assertIsNone(apt.has_balcony)


class TestStructuredGuardsAndRawAdapter(unittest.TestCase):
    """Verify GPS bounds, `$undefined` safety, `/mes.` rental gate, and panel condition guard."""

    def test_slovakia_gps_bounds(self) -> None:
        self.assertTrue(is_within_slovakia_gps(48.1486, 17.1077))   # Bratislava
        self.assertTrue(is_within_slovakia_gps(48.7164, 21.2611))   # Košice
        self.assertTrue(is_within_slovakia_gps(None, None))         # Omitted
        self.assertTrue(is_within_slovakia_gps(0.0, 0.0))           # Null-island placeholder
        self.assertFalse(is_within_slovakia_gps(43.5147, 16.2500))  # Croatia (Čiovo)
        self.assertFalse(is_within_slovakia_gps(18.5601, -68.3725)) # Dominican Republic

    def test_raw_adapter_strict_undefined_handling(self) -> None:
        ad = {
            "id": "JuUndef1",
            "isActive": True,
            "title": "1-izbový byt",
            "description": "$undefined",
            "location": {
                "country": "Slovensko",
                "city": "Nitra",
                "district": "okres Nitra",
                "point": {"latitude": 48.3069, "longitude": 18.0864},
            },
            "parameters": {
                "transaction": "Predaj",
                "area": "38",
                "floor": 2,
                "numberOfFloors": 3,
                "hasElevator": "$undefined",
                "construction": "Panelová",
                "yearOfConstruction": 1978,
                "realEstateState": "Pôvodný stav",
                "category": {"mainValue": "APARTMENTS", "subValue": "ONE_ROOM_APARTMENT"},
                "price": {"priceNum": 110_000, "price": "110 000 €", "unitPrice": "2 894,74 €/m²"},
                "attributes": [],
            },
        }
        raw = parse_apartment_desc_dict(ad)
        assert raw is not None
        self.assertFalse(raw["has_elevator"])
        self.assertEqual(raw["description"], "")

        apt = ApartmentModel(**raw)
        self.assertFalse(apt.has_elevator)
        self.assertEqual(apt.property_condition, "Pôvodný stav")
        self.assertEqual(apt.district, "Nitra")

    def test_panel_building_never_upgraded_to_novostavba(self) -> None:
        apt = ApartmentModel(
            id="JuPanelGuard",
            url="https://www.nehnutelnosti.sk/detail/JuPanelGuard/x",
            title="2-izbový byt pri Slnečnice Market",
            description="Byt v zateplenom dome pri projekte Slnečnice.",
            category_sub="TWO_ROOM_APARTMENT",
            property_condition="Pôvodný stav",
            construction="Panelová",
            year_of_construction=1982,
            city="Bratislava-Petržalka",
            district="Bratislava V",
        )
        self.assertEqual(apt.property_condition, "Pôvodný stav")

    def test_structured_monthly_rent_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuRentMes",
                url="https://www.nehnutelnosti.sk/detail/JuRentMes/x",
                title="Moderný 2-izbový byt v centre",
                category_sub="TWO_ROOM_APARTMENT",
                price_eur=850,
                price_display="850 €/mes.",
                city="Bratislava-Staré Mesto",
                district="Bratislava I",
            )

    def test_dynamic_future_completion_year_marks_vo_vystavbe(self) -> None:
        apt = ApartmentModel(
            id="JuFutureBuild",
            url="https://www.nehnutelnosti.sk/detail/JuFutureBuild/x",
            title="3-izbový byt v projekte",
            description="Plánované dokončenie: Q4 2028.",
            category_sub="THREE_ROOM_APARTMENT",
            property_condition="Novostavba",
            updated_at=datetime(2026, 11, 15, tzinfo=UTC),
            city="Bratislava-Ružinov",
            district="Bratislava II",
        )
        self.assertEqual(apt.property_condition, "Vo výstavbe")

    def test_bratislava_gps_spoofing_rejected(self) -> None:
        self.assertTrue(is_within_bratislava_gps(48.1486, 17.1077))
        self.assertFalse(is_within_bratislava_gps(48.2190, 17.4000))  # Senec

        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuSpoofGps",
                url="https://www.nehnutelnosti.sk/detail/JuSpoofGps/x",
                title="2-izbový byt v novostavbe",
                category_sub="TWO_ROOM_APARTMENT",
                city="Bratislava-Ružinov",
                district="Bratislava II",
                latitude=48.2190,
                longitude=17.4000,
            )

    def test_spider_sitemap_lifecycle_tracks_queued_and_expired_ids(self) -> None:
        spider = ApartmentSitemapSpider()
        spider.existing_timestamps = {
            "JuUnchanged": datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
            "JuUpdated": datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
        }
        entries = [
            {
                "loc": "https://www.nehnutelnosti.sk/detail/JuUnchanged/byt-1",
                "lastmod": "2026-11-20T12:00:00Z",
            },
            {
                "loc": "https://www.nehnutelnosti.sk/detail/JuUpdated/byt-2",
                "lastmod": "2026-11-21T12:00:00Z",
            },
        ]
        queued = list(spider.sitemap_filter(entries))
        self.assertEqual(len(queued), 1)
        # Both unchanged AND queued entries must be recorded in sitemap_seen_ids
        self.assertEqual(spider.sitemap_seen_ids, ["JuUnchanged", "JuUpdated"])
        self.assertEqual(spider.seen_unchanged_ids, ["JuUnchanged"])

        # Expired HTML banner must record the URL's ad_id in confirmed_inactive_ids
        expired_resp = HtmlResponse(
            url="https://www.nehnutelnosti.sk/detail/JuUpdated/byt-2",
            body="<html><body>Tento inzerát už nie je aktuálny</body></html>".encode(),
            encoding="utf-8",
        )
        self.assertEqual(spider.parse_apartment(expired_resp), [])
        self.assertIn("JuUpdated", spider.confirmed_inactive_ids)

    def test_spider_utc_and_id_helpers(self) -> None:
        self.assertEqual(
            extract_ad_id_from_url("https://www.nehnutelnosti.sk/detail/Ju123ABC/2-izbovy-byt"),
            "Ju123ABC",
        )
        dt = _parse_utc_timestamp("2026-11-18T10:30:00Z")
        assert dt is not None
        self.assertEqual(dt.tzinfo, UTC)
        self.assertEqual(dt.year, 2026)


if __name__ == "__main__":
    unittest.main()
