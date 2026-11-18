"""Unit tests for the resilient Next.js RSC Flight payload extractor (`src.rsc_extractor`)."""

import json
import unittest
from typing import Any

from src.rsc_extractor import (
    BLOAT_TOP_LEVEL_KEYS,
    extract_ad_dict,
    extract_ad_with_status,
    find_ad_script,
    strip_bloat_fields,
)


def _wrap_flight_html(scripts: list[str], extra_head: str = "") -> str:
    script_tags = "\n".join(f"<script>{sc}</script>" for sc in scripts)
    return f"<html><head>{extra_head}</head><body>{script_tags}</body></html>"


def _make_push(chunk_str: str, trailing_semicolon: bool = False) -> str:
    payload = json.dumps([1, chunk_str], ensure_ascii=False)
    suffix = ";" if trailing_semicolon else ""
    return f"self.__next_f.push({payload}){suffix}"


class TestRscExtractor(unittest.TestCase):
    """Verify wire-protocol edge cases in Next.js RSC Flight extraction."""

    def test_trailing_semicolon_and_nested_advertisement_location(self) -> None:
        ad_payload: dict[str, Any] = {
            "id": "JuNested1",
            "isActive": True,
            "title": "2-izbový byt",
            "description": "Krásny slnečný byt.",
        }
        # Deeply nested instead of hardcoded `data[3]["advertisement"]`
        flight_tree = ["$", "div", None, {"children": [{"props": {"advertisement": ad_payload}}]}]
        flight_line = f"1a:{json.dumps(flight_tree, ensure_ascii=False)}\n"
        html = _wrap_flight_html([_make_push(flight_line, trailing_semicolon=True)])

        self.assertIsNotNone(find_ad_script(html))
        ad, status = extract_ad_with_status(html)
        self.assertEqual(status, "ok")
        assert ad is not None
        self.assertEqual(ad["id"], "JuNested1")
        self.assertEqual(ad["description"], "Krásny slnečný byt.")

    def test_multiline_rsc_text_reference_respects_utf8_byte_length(self) -> None:
        ad_payload = {
            "id": "JuMultiRef",
            "isActive": True,
            "title": "3-izbový byt",
            "description": "$2f",
        }
        flight_line = f'1c:{json.dumps([0, 1, 2, {"advertisement": ad_payload}])}\n'
        real_desc = "Podlahová plocha bytu je 68,5 m² s balkónom."
        byte_len_hex = format(len(real_desc.encode("utf-8")), "x")
        # Another Flight chunk appears before AND after the referenced text record in the same script
        combined_chunk = (
            '1a:{"other":1}\n'
            f"2f:T{byte_len_hex},{real_desc}"
            '\n3a:["$","footer",null,{}]\n'
        )
        html = _wrap_flight_html([_make_push(flight_line), _make_push(combined_chunk)])

        ad = extract_ad_dict(html)
        assert ad is not None
        self.assertEqual(ad["description"], real_desc)

    def test_split_script_rsc_text_reference(self) -> None:
        ad_payload = {
            "id": "JuSplitRef",
            "isActive": True,
            "title": "1-izbový byt",
            "description": "$3b",
        }
        flight_line = f'1c:{json.dumps([0, 1, 2, {"advertisement": ad_payload}])}\n'
        real_desc = "Kompletne zrekonštruovaný byt bez balkóna."
        byte_len_hex = format(len(real_desc.encode("utf-8")), "x")
        html = _wrap_flight_html([
            _make_push(flight_line),
            _make_push(f"3b:T{byte_len_hex},"),
            _make_push(real_desc),
        ])

        ad = extract_ad_dict(html)
        assert ad is not None
        self.assertEqual(ad["description"], real_desc)

    def test_jsonld_fallback_when_rsc_ref_missing(self) -> None:
        ad_payload = {
            "id": "JuLdFallback",
            "isActive": True,
            "title": "2-izbový byt",
            "description": "$99",
        }
        flight_line = f'1c:{json.dumps([0, 1, 2, {"advertisement": ad_payload}])}\n'
        ld_json = json.dumps({"@type": "Apartment", "description": "JSON-LD fallback text"})
        extra_head = f'<script type="application/ld+json">{ld_json}</script>'
        html = _wrap_flight_html([_make_push(flight_line)], extra_head=extra_head)

        ad = extract_ad_dict(html)
        assert ad is not None
        self.assertEqual(ad["description"], "JSON-LD fallback text")

    def test_inactive_flag_and_expired_banner(self) -> None:
        ad_payload = {"id": "JuInactive1", "isActive": False, "title": "Predaný byt"}
        flight_line = f'1c:{json.dumps([0, 1, 2, {"advertisement": ad_payload}])}\n'
        html = _wrap_flight_html([_make_push(flight_line)])

        ad, status = extract_ad_with_status(html)
        self.assertEqual(status, "inactive")
        self.assertIsNotNone(ad)
        self.assertIsNone(extract_ad_dict(html))

        expired_html = "<html><body><h1>Tento inzerát už nie je aktuálny</h1></body></html>"
        ad2, status2 = extract_ad_with_status(expired_html)
        self.assertEqual(status2, "inactive")
        self.assertIsNone(ad2)

    def test_bot_challenge_detection(self) -> None:
        cf_html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body><div id='cf-browser-verification'>Checking browser</div></body></html>"
        )
        ad, status = extract_ad_with_status(cf_html)
        self.assertEqual(status, "bot_challenge")
        self.assertIsNone(ad)

    def test_strip_bloat_fields_preserves_core_data(self) -> None:
        raw = {
            "id": "JuTrim",
            "title": "Byt",
            "parameters": {"area": "55"},
            "similarAdvertisements": [{"id": "x"}] * 20,
            "media": [{"url": "https://img.example/1.jpg"}] * 30,
            "pois": [{"name": "Shop"}] * 50,
        }
        trimmed = strip_bloat_fields(raw)
        self.assertEqual(trimmed["id"], "JuTrim")
        self.assertEqual(trimmed["parameters"], {"area": "55"})
        for bloat_key in BLOAT_TOP_LEVEL_KEYS:
            self.assertNotIn(bloat_key, trimmed)


if __name__ == "__main__":
    unittest.main()
