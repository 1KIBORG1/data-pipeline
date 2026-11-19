"""Price fallback, exact portal area recovery, decimal typo repair, and interior area decomposition."""

from __future__ import annotations

import re
from typing import Any

from src.domain.reference import normalize_text, parse_positive_float
from src.domain.sections import split_description_sections


def compute_exact_portal_area(
    raw_area: float | None,
    price_num: float | None,
    unit_price_raw: Any,
) -> float | None:
    """Recover exact unrounded decimal area from portal `priceNum / unitPrice`.

    Nehnutelnosti.sk computes `unitPrice` from the exact float area (e.g. `39.55 m²`),
    while rounding `parameters.area` to an integer (`40`). Recovering the exact float
    improves sum checks (`interior + outdoor + cellar ~= exact_area`).
    """
    unit_price = parse_positive_float(unit_price_raw)
    if price_num is None or unit_price is None or price_num < 15_000 or unit_price < 300:
        return raw_area
    implied = round(price_num / unit_price, 2)
    if raw_area is not None and abs(implied - raw_area) <= 0.55:
        return implied
    return raw_area


def extract_price_from_text(text: str | None) -> int | None:
    """Extract fallback purchase price (EUR) from title when portal priceNum is omitted."""
    if not text:
        return None
    cleaned = normalize_text(text)
    cleaned = re.sub(
        r"\b(?:zlav\w*|usetrit\w*|uspor\w*|parkov\w*|garaz\w*|statie|provizi\w*|odmen\w*)\s*(?:vo\s+vyske\s*|len\s*|za\s*)?\d[\d\s.,]*\s*(?:€|eur\b)",
        " ",
        cleaned,
    )
    for m in re.finditer(
        r"(?<![.,\d])(\d{2,3}(?:[\s\u00a0.]?\d{3})+|\d{5,7})\s*(?:€|eur\b)(?!\s*/\s*(?:mes|m2|m²))",
        cleaned,
    ):
        raw_digits = re.sub(r"[\s\u00a0.]+", "", m.group(1))
        if raw_digits.isdigit():
            val = int(raw_digits)
            if 18_000 <= val <= 5_000_000:
                return val
    return None


def extract_area_from_text(
    text: str | None,
    is_title: bool = False,
    min_area: float = 12.0,
) -> float | None:
    """Extract apartment floor area (m2) from title or description when missing or invalid."""
    if not text:
        return None

    cleaned = normalize_text(text)

    m_labeled = re.search(
        r"(?:"
        r"\b(?:vymera|rozloha|uzitkova\s+plocha|podlahova\s+plocha|celkova\s+vymera)\s+(?:bytu|apartmanu|garsonky|mezonetu)?\s*(?:je|ma|predstavuje|[:=-])?\s*(?:cca\s*|velkorysych\s*|az\s*)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\b(?:byt|apartman|garsonka|mezonet)\s*(?:ma\s+(?:vymeru|rozlohu)|o\s+vymere|s\s+vymerou|o\s+rozlohe|s\s+rozlohou|s\s+uzitkovou\s+plochou|\()\s*(?:cca\s*)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\b(?:vymera|uzitkova\s+plocha|podlahova\s+plocha)\s*[:=-]\s*(?:cca\s*|velkorysych\s*)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r")",
        cleaned,
    )
    if m_labeled:
        raw_num = m_labeled.group(1) or m_labeled.group(2) or m_labeled.group(3)
        val = float(raw_num.replace(",", "."))
        if max(12.0, min_area) <= val <= 400.0:
            return round(val, 2)

    if is_title:
        ext_words = (
            "pozem",
            "zahrad",
            "predzahrad",
            "teras",
            "balkon",
            "loggi",
            "lodzi",
            "logi",
            "garaz",
            "pivnic",
            "exterier",
            "ulozn",
            "poval",
            "nebytov",
            "sklad",
            "kobk",
        )
        for m in re.finditer(r"(?<![.,\d])(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)\b", cleaned):
            val = float(m.group(1).replace(",", "."))
            prefix = re.split(r"[\d+|,/]", cleaned[max(0, m.start() - 25) : m.start()])[-1]
            suffix_raw = cleaned[m.end() : min(len(cleaned), m.end() + 25)]
            if re.search(r"\bo\s*$", prefix) or re.match(r"^\s*viac\b", suffix_raw):
                continue
            suffix = re.split(r"[\d+|,/]|\b(?:s|so|a)\s+", suffix_raw)[0]
            if any(w in f"{prefix} {suffix}" for w in ext_words):
                continue
            if max(12.0, min_area) <= val <= 400.0:
                return round(val, 2)

    return None


def fix_missing_decimal_area(
    area_sqm: float | None,
    rooms_count: int | None,
    price_eur: int | None,
    min_area: float,
) -> float | None:
    """Correct missing-decimal typos on 1-2 room listings (e.g. 392 -> 39.2, 654 -> 65.4) safely.

    Runs BEFORE the 600 m² upper-bound cutoff and validates price/m² plausibility so genuine
    215 m² luxury lofts are never divided by 10.
    """
    if area_sqm is None:
        return None
    if rooms_count in (1, 2) and 200.0 < area_sqm < 1000.0 and int(area_sqm) % 10 != 0:
        cand = round(area_sqm / 10.0, 2)
        if min_area <= cand <= 100.0:
            if price_eur is None:
                return cand
            pps_orig = price_eur / area_sqm
            pps_cand = price_eur / cand
            if 500.0 <= pps_cand <= 15_000.0 and (pps_orig < 1_500.0 or area_sqm > 350.0):
                return cand
    return area_sqm


def resolve_pure_apartment_area(
    raw_area: float | None,
    min_area: float,
    title: str | None = None,
    description: str | None = None,
    exterior_area: float | None = None,
    exact_portal_area: float | None = None,
) -> float | None:
    """Resolve pure interior apartment floor area (`Podlahová plocha / Interiér`) in m2.

    Uses exact portal area (`priceNum / unitPrice`) when available to check whether
    `interior + ext_sum ~= portal_area`, scoped to non-location description sections.
    """
    ref_area = exact_portal_area if exact_portal_area is not None else raw_area
    sections = split_description_sections(description)
    t_clean = normalize_text(title)
    d_clean = normalize_text(sections.non_location_text)
    combined = f"{t_clean}\n{d_clean}".strip()

    if not combined:
        return ref_area

    # Strip clauses describing an adjacent/additional buyable unit ("dokúpiť aj vedľajší byt 164 m²")
    combined_safe = re.sub(
        r"\b(?:dokupit|prikupit|moznost\s+kupit)\s+[^.;\n]{0,120}?(?:vedlajs\w*|susedn\w*|druh\w*)\s+byt[^.;\n]{0,80}",
        " ",
        combined,
    )
    combined_safe = re.sub(
        r"\b(?:vedlajs\w+|susedn\w+)\s+byt\s+[^.;\n]{0,100}",
        " ",
        combined_safe,
    )

    # 1. Explicit "podlahová plocha" / "čistá plocha bytu" / "interiér" / "samotný byt"
    for m_podl in re.finditer(
        r"(?:"
        r"\b(?:podlahova|cista|vnutorna|obytna)\s+(?:plocha|vymera)(?:\s+bytu|\s+apartmanu)?\s*(?:je|predstavuje|ma|[:=-])?\s*(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\binterier(?:\s+bytu)?\s*(?:je|ma|[:=-])?\s*(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\b(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s+interier\s*\+\s*(\d{1,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s+exterier"
        r"|\bsamotn\w+\s+byt\s+(?:je\s+|ma\s+)?(?:o\s+vymere\s+|vymeru\s+)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\bz\s+(?:coho|toho)\s+(?:vnutorna\s+plocha[^.;\n]{0,45}?je\s+|az\s+|je\s+)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s*(?:(?:obytnej|podlahovej|cistej)\s+plochy|\+\s*(?:zasklen\w+\s+)?(?:balkon|loggi|lodzi|teras))"
        r")",
        combined_safe,
    ):
        cand_str = (
            m_podl.group(1)
            or m_podl.group(2)
            or m_podl.group(3)
            or m_podl.group(5)
            or m_podl.group(6)
        )
        if not cand_str:
            continue
        cand = float(cand_str.replace(",", "."))
        after_ctx = combined_safe[m_podl.end() : m_podl.end() + 45]
        if re.match(r"^\s*(?:,\s*)?vratane\s+(?:loggi|lodzi|balkon|teras)", after_ctx):
            continue
        if not (min_area <= cand <= 400.0):
            continue
        if ref_area is None:
            return round(cand, 2)
        if cand < ref_area:
            ext_inline = float(m_podl.group(4).replace(",", ".")) if m_podl.group(4) else 0.0
            m_after_ext = re.search(
                r"(?:teras\w*|loggi\w*|lodzi\w*|balkon\w*|pivnic\w*|predzahrad\w*)\s*(?:ma\s+rozlohu\s*|o\s+vymere\s*|[:=-]\s*)?(\d{1,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
                r"|(\d{1,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s*(?:teras\w*|loggi\w*|lodzi\w*|balkon\w*|pivnic\w*|predzahrad\w*)",
                after_ctx,
            )
            if not ext_inline and m_after_ext:
                ext_inline = float(
                    (m_after_ext.group(1) or m_after_ext.group(2)).replace(",", ".")
                )
            if (
                cand >= ref_area * 0.45
                or (ext_inline > 0 and abs((cand + ext_inline) - ref_area) <= 5.0)
                or (exterior_area and abs((cand + exterior_area) - ref_area) <= 5.0)
            ):
                return round(cand, 2)

    # 2. Explicit additive breakdown: "<A_apt> m2 [byt] +/, [<A_ext> m2] (balkón|loggia|terasa|pivnica|predzáhradka)"
    room_words = (
        "izba",
        "izby",
        "spaln",
        "kuchyn",
        "kupeln",
        "chodb",
        "predsien",
        "obyvac",
        "detsk",
        "hal",
        "pracovn",
        "satnik",
        "wc",
        "toalet",
    )
    for m_add in re.finditer(
        r"(?:"
        r"(?:rozloha|vymera|plocha|up|uzitkova\s+plocha|dispozicia|interier|byt\w*|apartman\w*|garson\w*|mezonet\w*|penthouse)\s*(?:bytu\s*|o\s+rozlohe\s*|o\s+vymere\s*|s\s+vymerou\s*|s\s+prislusenstvom\s*)?(?:je\s+|ma\s+|predstavuje\s+|[:=(-]\s*)?"
        r")?"
        r"(?<![.,\d])(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)(?:\s+byt)?"
        r"(?:\s*,?\s*\(?\s*vratane\s+pivnice(?:\s+na\s+poschodi)?(?:\s+\d+(?:[.,]\d+)?\s*(?:m2|m²))?\s*\)?)?"
        r"\s*(?:\+|a\s+(?:zasklen\w+\s+)?|,?\s*s\s+(?:zasklen\w+\s+|velk\w+\s+|priestrann\w+\s+)?|,?\s*so\s+(?:zasklen\w+\s+)?|,?\s*disponuje\s+|,\s*(?:plocha\s+)?)"
        r"\s*(?:(\d{1,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s*)?"
        r"(?:zasklen\w+\s+)?"
        r"(balkon\w*|bakon\w*|loggi\w*|lodzi\w*|logi\w*|teras\w*|pivnic\w*|predzahrad\w*)"
        r"(?:\s*(?:o\s+vymere\s*|s\s+vymerou\s*|s\s+rozlohou\s*|ma\s+rozlohu\s*|[:=-]\s*)?(\d{1,3}(?:[.,]\d+)?)\s*(?:m2|m²|m\b))?",
        combined_safe,
    ):
        cand_apt = float(m_add.group(1).replace(",", "."))
        if not (min_area <= cand_apt <= 400.0):
            continue
        prefix_ctx = re.split(r"[\n.;!]", combined_safe[max(0, m_add.start() - 30) : m_add.start()])[
            -1
        ]
        if any(rw in prefix_ctx for rw in room_words):
            continue

        ext1 = float(m_add.group(2).replace(",", ".")) if m_add.group(2) else 0.0
        ext2 = float(m_add.group(4).replace(",", ".")) if m_add.group(4) else 0.0
        tail_ctx = combined_safe[m_add.end() : m_add.end() + 50]
        m_tail = re.match(
            r"^\s*(?:\+|,)\s*(?:(\d{1,2}(?:[.,]\d+)?)\s*(?:m2|m²)\s*(?:pivnic\w*|kobk\w*|balkon\w*|loggi\w*|lodzi\w*)|(?:pivnic\w*|kobk\w*|balkon\w*|loggi\w*|lodzi\w*)\s*(?:na\s+poschodi\s*)?(\d{1,2}(?:[.,]\d+)?)\s*(?:m2|m²))",
            tail_ctx,
        )
        ext3 = float((m_tail.group(1) or m_tail.group(2)).replace(",", ".")) if m_tail else 0.0
        ext_sum = ext1 + ext2 + ext3
        if ext_sum == 0.0 and exterior_area:
            ext_sum = exterior_area

        if ref_area is None:
            return round(cand_apt, 2)
        if cand_apt < ref_area - 0.4:
            full_match_str = m_add.group(0)
            is_labeled = any(
                lbl in full_match_str
                for lbl in (
                    "rozloha",
                    "vymera",
                    "plocha",
                    "up ",
                    "dispozicia",
                    "interier",
                    "byt",
                    "mezonet",
                    "penthouse",
                )
            )
            conserves_area = ext_sum > 0 and abs((cand_apt + ext_sum) - ref_area) <= 4.0
            if conserves_area or (
                cand_apt >= ref_area * 0.40 and (is_labeled or m_add.start() <= len(t_clean))
            ):
                return round(cand_apt, 2)

    # 3. Explicit subtraction: "celková výmera 70.00 m2 z toho loggia 3.78 m2"
    m_sub = re.search(
        r"\b(?:celkov\w+\s+)?(?:vymer\w+|rozloh\w+|ploch\w+)(?:\s+bytu)?\s*(?:je\s+)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)\s*(?:,\s*)?(?:z\s+toho|vratane)\s+"
        r"(?:(?:loggi\w*|lodzi\w*|balkon\w*|teras\w*|pivnic\w*)\s*(?:o\s+vymere\s*|s\s+vymerou\s*)?(\d{1,2}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|(\d{1,2}(?:[.,]\d+)?)\s*(?:m2|m²)\s*(?:loggi\w*|lodzi\w*|balkon\w*|teras\w*|pivnic\w*)"
        r"|(?:loggi\w*|lodzi\w*|balkon\w*|teras\w*))",
        combined_safe,
    )
    if m_sub:
        gross = float(m_sub.group(1).replace(",", "."))
        sub_str = m_sub.group(2) or m_sub.group(3)
        sub_val = float(sub_str.replace(",", ".")) if sub_str else (exterior_area or 0.0)
        if sub_val > 0 and min_area <= (gross - sub_val) < (ref_area or 999.0):
            return round(gross - sub_val, 2)

    # 4. Fallback to title/description area if raw_area is missing or grossly inflated
    title_area = extract_area_from_text(title, is_title=True, min_area=min_area)
    if raw_area is None:
        return title_area or extract_area_from_text(
            sections.non_location_text, is_title=False, min_area=min_area
        )
    if (
        title_area is not None
        and title_area >= min_area
        and raw_area > max(title_area * 1.35, title_area + 25.0)
    ):
        return title_area

    return ref_area
