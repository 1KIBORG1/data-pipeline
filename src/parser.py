"""Adapter converting unpacked Nehnutelnosti.sk RSC payloads into raw item dicts.

HTML/RSC unpacking lives in `src.rsc_extractor`; domain cleaning and validation
live in `src.models` and `src.domain`.
"""

from __future__ import annotations

import re
from typing import Any

from src.domain.amenities import (
    build_attrs_map,
    compute_exterior_area_from_attrs,
    has_elevator_from_attrs,
    resolve_balcony_from_attrs,
)
from src.domain.reference import parse_positive_float
from src.rsc_extractor import (
    extract_ad_dict,
    extract_ad_with_status,
    find_ad_script,
    strip_bloat_fields,
)

__all__ = [
    "build_attrs_map",
    "compute_exterior_area_from_attrs",
    "extract_ad_dict",
    "extract_ad_with_status",
    "find_ad_script",
    "has_balcony_from_attrs",
    "has_elevator_from_attrs",
    "parse_apartment_desc_dict",
    "parse_apartment_html",
    "resolve_balcony_from_attrs",
    "strip_bloat_fields",
]


def has_balcony_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool:
    """Return True if structured attributes confirm a balcony, loggia, or terrace."""
    return resolve_balcony_from_attrs(attrs, vybavenie) is True


def _clean_raw_str(value: Any) -> str | None:
    """Return stripped string or None for empty values and Next.js `$undefined` placeholders."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped == "$undefined":
        return None
    return stripped


def _parse_coord(value: Any) -> float | None:
    """Parse a signed WGS84 latitude/longitude coordinate safely."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().replace(",", ".")
        if not stripped or stripped == "$undefined":
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _parse_year(value: Any) -> int | None:
    """Extract a 4-digit construction year (`1800..2040`) from an int or string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1800 <= value <= 2040 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "$undefined":
            return None
        match = re.search(r"\b(1[89]\d{2}|20[0-4]\d)\b", stripped)
        if match:
            return int(match.group(1))
    return None


def parse_apartment_desc_dict(ad: dict[str, Any], url: str = "") -> dict[str, Any] | None:
    """Adapt an unpacked `advertisement` JSON dict into a raw item dict for `ApartmentModel`.

    Performs only fast structural scope checks (`APARTMENTS`, `Slovensko`, active status)
    and extracts raw portal fields without applying lossy text heuristics.
    """
    if not ad or not ad.get("id"):
        return None
    if ad.get("isActive") is False:
        return None
    if "status" in ad and ad.get("status") not in (None, "ACTIVE"):
        return None

    loc = ad.get("location") or {}
    point = loc.get("point") or {}
    params = ad.get("parameters") or {}
    category = params.get("category") or {}
    price_obj = params.get("price") or {}
    attrs = build_attrs_map(params.get("attributes"))

    # Keep only apartment listings located in Slovakia
    if category.get("mainValue") != "APARTMENTS":
        return None
    country = _clean_raw_str(loc.get("country"))
    if country and country != "Slovensko":
        return None

    final_url = url or (
        f"https://www.nehnutelnosti.sk/detail/{ad['id']}/{ad.get('sefName', '')}"
    )

    title_raw = ad.get("title")
    if isinstance(title_raw, list):
        title: str | None = " ".join(str(x) for x in title_raw if x)
    else:
        title = _clean_raw_str(title_raw)

    description = ad.get("description") if isinstance(ad.get("description"), str) else ""
    if description == "$undefined":
        description = ""

    umiestnenie = attrs.get("Umiestnenie", "")
    vybavenie = attrs.get("Vybavenie", "")

    floor_raw = params.get("floor")
    if floor_raw is None or floor_raw == "$undefined":
        floor_raw = attrs.get("Poschodie") or umiestnenie or None

    total_floors_raw = params.get("numberOfFloors")
    if total_floors_raw is None or total_floors_raw == "$undefined":
        total_floors_raw = attrs.get("Počet nadzemných podlaží")

    # Strict `is True` check avoids truthy Next.js `"$undefined"` placeholder strings
    has_elevator_raw = (params.get("hasElevator") is True) or has_elevator_from_attrs(
        attrs, vybavenie
    )
    has_balcony_raw = resolve_balcony_from_attrs(attrs, vybavenie)

    condition_raw = _clean_raw_str(params.get("realEstateState")) or attrs.get(
        "Stav nehnuteľnosti"
    )
    construction_raw = _clean_raw_str(params.get("construction")) or attrs.get("Konštrukcia")

    return {
        "id": str(ad["id"]),
        "url": final_url,
        "title": title,
        "transaction_type": _clean_raw_str(params.get("transaction")),
        "category_sub": _clean_raw_str(category.get("subValue")),
        "rooms_count": attrs.get("Počet izieb / miestností"),
        "price_eur": price_obj.get("priceNum"),
        "price_per_sqm_eur": price_obj.get("unitPrice"),
        "area_sqm": parse_positive_float(params.get("area")),
        "floor": floor_raw,
        "total_floors": total_floors_raw,
        "property_condition": condition_raw,
        "has_elevator": has_elevator_raw,
        "has_balcony": has_balcony_raw,
        "city": _clean_raw_str(loc.get("city")),
        "district": _clean_raw_str(loc.get("district")),
        "updated_at": ad.get("updatedAt"),
        # Context fields consumed by ApartmentModel validation (excluded from DB dump)
        "description": description,
        "umiestnenie": umiestnenie,
        "vlastnictvo": attrs.get("Vlastníctvo", ""),
        "street": _clean_raw_str(loc.get("street")),
        "latitude": _parse_coord(point.get("latitude")),
        "longitude": _parse_coord(point.get("longitude")),
        "price_display": _clean_raw_str(price_obj.get("price")),
        "price_note": _clean_raw_str(price_obj.get("noteToPrice")),
        "construction": construction_raw,
        "year_of_construction": _parse_year(params.get("yearOfConstruction")),
        "year_of_construction_lookup": _clean_raw_str(params.get("yearOfConstructionLookup")),
        "rok_kolaudacie": parse_positive_float(attrs.get("Rok kolaudácie")),
        "plocha_pozemku": parse_positive_float(attrs.get("Plocha pozemku")),
        "zastavana_plocha": parse_positive_float(attrs.get("Zastavaná plocha")),
        "exterior_area": compute_exterior_area_from_attrs(attrs),
        "is_active": ad.get("isActive") is not False,
    }


def parse_apartment_html(html: str, url: str = "") -> dict[str, Any] | None:
    """Extract raw apartment fields from listing HTML for downstream Pydantic validation."""
    ad = extract_ad_dict(html)
    if not ad:
        return None
    return parse_apartment_desc_dict(ad, url=url)
