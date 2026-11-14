"""HTML and Next.js RSC payload parser for Nehnutelnosti.sk apartment listings.

Nehnutelnosti.sk uses the Next.js App Router with React Server Components (RSC).
The structured advertisement object is embedded inside a `<script>` tag as a
serialized RSC Flight chunk (`self.__next_f.push([1, "..."])`).
"""

import json
import re
from typing import Any

from parsel import Selector

from src.models import (
    _normalize_dashes,
    _remove_diacritics,
    extract_area_from_text,
    extract_floor_and_total_from_text,
    extract_rooms_from_title,
    has_balcony_from_text,
    has_elevator_from_text,
    has_negated_balcony,
    has_negated_elevator,
    infer_property_condition,
    parse_floor,
    parse_positive_float,
)

# Foreign streets mistakenly placed under Slovak border districts on the portal
FOREIGN_STREETS = ("am strandbad", "thulusdhoo")


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
    Layer 3: If `advertisement.description` is a Flight chunk pointer (e.g. `"$2f"`),
             resolve the full description string from the corresponding `<script>` chunk.
    """
    sel = Selector(text=html)
    ad_script = sel.xpath(
        """//script[contains(text(), 'self.__next_f.push(') and contains(text(), '\\"advertisement\\":{\\"id\\"')]/text()"""
    ).get()
    if not ad_script:
        return None

    text = ad_script.strip()
    try:
        # Unpack outer JS array: self.__next_f.push([1, "..."]) -> [1, "..."]
        payload = json.loads(text[19:-1])
        line = next(
            line for line in payload[1].splitlines()
            if '{"advertisement":{"id"' in line
        )
        # Strip RSC Flight chunk ID prefix (e.g. "2b:") before decoding inner JSON
        data = json.loads(line[line.index(":") + 1:])
        ad: dict[str, Any] = data[3]["advertisement"]

        # Resolve out-of-line RSC Flight text chunk for long descriptions (e.g. "$2f")
        desc = ad.get("description")
        if isinstance(desc, str) and desc.startswith("$") and len(desc) > 1:
            ref_id = desc[1:]
            chunk_script = sel.xpath(
                f"""//script[contains(text(), 'self.__next_f.push([1,"{ref_id}:T')]/following-sibling::script[1]/text()"""
            ).get()
            if chunk_script:
                chunk_str = chunk_script.strip()
                if chunk_str.startswith("self.__next_f.push("):
                    ad["description"] = json.loads(chunk_str[19:-1])[1]

        return ad

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


def resolve_balcony_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool | None:
    """Resolve three-valued balcony status (`True`, `False`, or `None`) from structured attributes.

    Returns:
        `True` if any balcony/loggia/terrace attribute is positive or listed in `Vybavenie`.
        `False` if balcony/loggia/terrace attributes are explicitly negated (`"0"`, `"Nie"`).
        `None` if no balcony/loggia/terrace attribute was filled in by the realtor.
    """
    vyb_clean = _remove_diacritics(vybavenie).casefold()
    if any(w in vyb_clean for w in ("balkon", "loggi", "lodzi", "teras")):
        return True

    saw_explicit_false = False
    for label, val in attrs.items():
        label_clean = _remove_diacritics(label).casefold()
        if any(w in label_clean for w in ("balkon", "loggi", "lodzi", "teras")):
            val_clean = _remove_diacritics(val).strip().casefold()
            if not val_clean or val_clean in ("-", "none"):
                continue
            if val_clean.startswith("0") or val_clean in ("nie", "false"):
                saw_explicit_false = True
            else:
                return True

    return False if saw_explicit_false else None


def has_balcony_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool:
    """Return True if structured attributes confirm a balcony, loggia, or terrace."""
    return resolve_balcony_from_attrs(attrs, vybavenie) is True


def has_elevator_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool:
    """Check structured attributes (`parameters.attributes`) for an elevator ('výťah')."""
    vyb_clean = _remove_diacritics(vybavenie).casefold()
    if "vytah" in vyb_clean:
        return True

    for label, val in attrs.items():
        label_clean = _remove_diacritics(label).casefold()
        if "vytah" in label_clean:
            val_clean = _remove_diacritics(val).strip().casefold()
            if (
                val_clean
                and not val_clean.startswith("0")
                and val_clean not in ("nie", "false", "-", "none")
            ):
                return True

    return False


def extract_rooms_from_description(description: str) -> int | None:
    """Extract room count from description when missing from attributes and title."""
    if not description:
        return None
    cleaned = _normalize_dashes(_remove_diacritics(description).casefold())
    # Ignore multi-unit developer descriptions ("2-izbove az 4-izbove byty", "4 jednoizbove byty")
    if re.search(r"\b\d+\s*-?\s*izb\w*\s+az\s+\d+\s*-?\s*izb", cleaned):
        return None
    m = re.search(
        r"\b(?:predaj|ponukame|kupim|predam|na\s+predaj)?\s*(?:kompletne\s+zariaden\w+\s+|zrekonstruovan\w+\s+|priestrann\w+\s+|slnecn\w+\s+|investicn\w+\s+)?"
        r"([1-6](?:[.,]5)?\s*(?:-\s*)?(?:izbov\w+|i-kk|izb\.)|(?:jedno|dvoj|troj|stvor|pat)izbov\w+)\s+"
        r"(?:investicn\w+\s+|rekreacn\w+\s+)?(?:byt|apartman|holobyt|mezonet|loft)\b",
        cleaned,
    )
    if m:
        return extract_rooms_from_title(m.group(1))
    return None


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
    street_clean = _remove_diacritics(str(loc.get("street") or "")).strip().casefold()
    if street_clean in FOREIGN_STREETS:
        return None

    final_url = url or (
        f"https://www.nehnutelnosti.sk/detail/"
        f"{ad['id']}/{ad.get('sefName', '')}"
    )
    title_raw = ad.get("title")
    if isinstance(title_raw, list):
        title = " ".join(str(x) for x in title_raw if x)
    else:
        title = title_raw
    description = ad.get("description") if isinstance(ad.get("description"), str) else ""

    # 1. Resolve floor and total_floors from structured parameters, attributes, title, and description
    umiestnenie = attrs.get("Umiestnenie", "")
    umiest_clean = _remove_diacritics(umiestnenie).casefold()
    floor_raw = params.get("floor")
    if floor_raw is None:
        floor_raw = attrs.get("Poschodie") or umiestnenie
    total_floors_raw = params.get("numberOfFloors") or attrs.get("Počet nadzemných podlaží")

    # Correct ground-floor listings where realtor entered floor=1, numberOfFloors=1 with Umiestnenie='Prízemie'
    if "prizemie" in umiest_clean and floor_raw == 1 and total_floors_raw in (1, None):
        floor_raw = 0

    parsed_fl = parse_floor(floor_raw)
    parsed_tot = parse_floor(total_floors_raw)

    if parsed_fl is None or parsed_tot is None:
        t_fl, t_tot = extract_floor_and_total_from_text(title)
        d_fl, d_tot = extract_floor_and_total_from_text(description)
        if parsed_fl is None:
            parsed_fl = t_fl if t_fl is not None else d_fl
        if parsed_tot is None:
            parsed_tot = t_tot if t_tot is not None else d_tot

    # Cross-fill top-floor apartments ("Posledné podlažie") when one of floor / total_floors is known
    if "posledne podlazie" in umiest_clean:
        if parsed_fl is None and parsed_tot is not None and parsed_tot >= 1:
            parsed_fl = parsed_tot
        elif parsed_tot is None and parsed_fl is not None and parsed_fl >= 2:
            parsed_tot = parsed_fl

    # 2. Derive elevator (bool) and three-valued balcony (`True` / `False` / `None`)
    vybavenie = attrs.get("Vybavenie", "")
    has_elevator = bool(params.get("hasElevator")) or has_elevator_from_attrs(attrs, vybavenie)
    if not has_elevator and not has_negated_elevator(title) and not has_negated_elevator(description):
        has_elevator = (
            has_elevator_from_text(title)
            or has_elevator_from_text(description)
            or (parsed_fl is not None and parsed_fl >= 6)
            or (parsed_tot is not None and parsed_tot >= 7)
        )

    attr_balcony = resolve_balcony_from_attrs(attrs, vybavenie)
    if attr_balcony is True:
        has_balcony: bool | None = True
    elif not has_negated_balcony(title) and (
        has_balcony_from_text(title)
        or has_balcony_from_text(final_url)
        or has_balcony_from_text(description)
    ):
        has_balcony = True
    elif (
        attr_balcony is False
        or has_negated_balcony(title)
        or has_negated_balcony(description)
    ):
        has_balcony = False
    else:
        has_balcony = None

    # 3. Resolve area_sqm with fallback to title, description, or Zastavaná plocha on typos
    area_val = parse_positive_float(params.get("area"))
    zastavana_val = parse_positive_float(attrs.get("Zastavaná plocha"))
    pozemok_val = parse_positive_float(attrs.get("Plocha pozemku"))

    # Recover missing-zero typo (e.g. params.area=8 vs Zastavaná=80, or params.area=11 vs Zastavaná=110)
    if (
        area_val is not None
        and area_val < 20.0
        and zastavana_val is not None
        and 25.0 <= zastavana_val <= 250.0
        and zastavana_val != pozemok_val
        and abs(zastavana_val - area_val * 10) <= 5.0
    ):
        area_val = zastavana_val

    if area_val is None or area_val < 10.0:
        area_val = (
            extract_area_from_text(title, is_title=True)
            or extract_area_from_text(description, is_title=False)
        )
    elif area_val >= 110.0:
        # Correct area_val if it mistakenly sums a garden/terrace/plot while description states interior apartment m2
        desc_area = extract_area_from_text(description, is_title=False)
        if (
            desc_area is not None
            and 15.0 <= desc_area <= 150.0
            and area_val > max(desc_area * 1.45, desc_area + 35.0)
        ):
            area_val = desc_area

    # 4. Resolve rooms_count with fallback to description for holiday/maisonette/loft apartments
    rooms_raw: Any = attrs.get("Počet izieb / miestností")
    if rooms_raw is None:
        rooms_raw = extract_rooms_from_description(description)

    # 5. Resolve property_condition with fallback to attributes, title, description, and construction year
    condition_raw = (
        params.get("realEstateState")
        or attrs.get("Stav nehnuteľnosti")
        or infer_property_condition(title)
        or infer_property_condition(description)
    )
    if not condition_raw:
        rok_kol = parse_positive_float(attrs.get("Rok kolaudácie") or attrs.get("Rok výstavby"))
        if rok_kol is not None and 2020 <= rok_kol <= 2030:
            condition_raw = "Novostavba"

    return {
        "id": ad["id"],
        "url": final_url,
        "title": title,
        "transaction_type": params.get("transaction"),
        "category_sub": category.get("subValue"),
        "rooms_count": rooms_raw,
        "price_eur": price_obj.get("priceNum"),
        "price_per_sqm_eur": price_obj.get("unitPrice"),
        "area_sqm": area_val,
        "floor": parsed_fl if parsed_fl is not None else floor_raw,
        "total_floors": parsed_tot if parsed_tot is not None else total_floors_raw,
        "property_condition": condition_raw,
        "has_elevator": has_elevator,
        "has_balcony": has_balcony,
        "city": loc.get("city"),
        "district": loc.get("district"),
        "updated_at": ad.get("updatedAt"),
    }
