"""HTML and Next.js RSC payload parser for Nehnutelnosti.sk apartment listings.

Nehnutelnosti.sk uses the Next.js App Router with React Server Components (RSC).
The structured advertisement object is embedded inside a `<script>` tag as a
serialized RSC Flight chunk (`self.__next_f.push([1, "..."])`).
"""

import json
from typing import Any

from parsel import Selector


def find_ad_script(html: str) -> str | None:
    """Locate the `<script>` tag containing the escaped advertisement JSON chunk."""
    sel = Selector(text=html)
    ad_script = sel.xpath(
        """//script[contains(text(), 'self.__next_f.push(') and contains(text(), '\\"advertisement\\":{\\"id\\"')]/text()"""
    ).get()
    return ad_script.strip() if ad_script else None


def extract_ad_dict(html: str) -> dict[str, Any] | None:
    """Unpack the two-layer Next.js RSC Flight payload and return the raw advertisement dict.

    Layer 1: Strip the `self.__next_f.push(` prefix (19 chars) and trailing `)` (1 char),
             then parse the outer JSON array `[1, "<flight_lines>"]`.
    Layer 2: Find the Flight line containing `{"advertisement":{"id"`, strip the hex
             chunk prefix before the first colon (e.g. `1a:[...]`), and parse the JSON tree.
    """
    text = find_ad_script(html)
    if text is None:
        return None

    try:
        # Unpack outer JS array: self.__next_f.push([1, "..."]) -> [1, "..."]
        payload = json.loads(text[19:-1])
        line = next(
            line for line in payload[1].splitlines()
            if '{"advertisement":{"id"' in line
        )
        # Strip RSC Flight chunk ID prefix (e.g. "2b:") before decoding inner JSON
        data = json.loads(line[line.index(":") + 1:])
        return data[3]["advertisement"]

    except (json.JSONDecodeError, IndexError, ValueError, TypeError, StopIteration):
        return None


def build_attrs_map(attrs_list: list[dict[str, Any]] | None) -> dict[str, str]:
    """Flatten `parameters.attributes` (`[{"label": ..., "value": ...}]`) into a lookup dict."""
    result: dict[str, str] = {}

    for item in attrs_list or []:
        label = item.get("label")
        value = item.get("value")

        # Ignore empty values and Next.js undefined placeholders
        if not label or value is None or value in ("", "$undefined"):
            continue

        result[str(label).strip()] = str(value).strip()

    return result


def parse_apartment_html(html: str, url: str = "") -> dict[str, Any] | None:
    """Extract raw apartment fields from listing HTML for downstream Pydantic validation.

    Returns `None` if the page has no valid advertisement payload, is not an
    apartment (`mainValue != "APARTMENTS"`), or is located outside Slovakia.
    """
    ad = extract_ad_dict(html)
    if not ad or not ad.get("id"):
        return None

    loc = ad.get("location") or {}
    params = ad.get("parameters") or {}
    category = params.get("category") or {}
    price_obj = params.get("price") or {}
    attrs = build_attrs_map(params.get("attributes"))

    # Keep only apartment listings located in Slovakia
    if category.get("mainValue") != "APARTMENTS":
        return None
    if loc.get("country") and loc.get("country") != "Slovensko":
        return None

    # Derive boolean flags from both structured parameters and free-text equipment attributes
    vybavenie = attrs.get("Vybavenie", "")
    has_elevator = bool(params.get("hasElevator")) or ("Výťah" in vybavenie)

    balcony_cnt = attrs.get("Počet balkónov") or attrs.get("Počet loggií")
    has_balcony = (
        any(w in vybavenie for w in ("Balkón", "Loggia", "Terasa"))
        or (balcony_cnt is not None and balcony_cnt not in ("0", "Nie", "nie"))
    )

    # Fall back to secondary attributes when top-level floor parameter is absent
    floor_raw = params.get("floor")
    if floor_raw is None:
        floor_raw = attrs.get("Poschodie") or attrs.get("Umiestnenie")

    return {
        "id": ad["id"],
        "url": url or (
            f"https://www.nehnutelnosti.sk/detail/"
            f"{ad['id']}/{ad.get('sefName', '')}"
        ),
        "title": ad.get("title"),
        "transaction_type": params.get("transaction"),
        "category_sub": category.get("subValue"),
        "rooms_count": attrs.get("Počet izieb / miestností"),
        "price_eur": price_obj.get("priceNum"),
        "price_per_sqm_eur": price_obj.get("unitPrice"),
        "area_sqm": params.get("area"),
        "floor": floor_raw,
        "total_floors": params.get("numberOfFloors"),
        "property_condition": params.get("realEstateState"),
        "has_elevator": has_elevator,
        "has_balcony": has_balcony,
        "city": loc.get("city"),
        "district": loc.get("district"),
        "updated_at": ad.get("updatedAt"),
    }
