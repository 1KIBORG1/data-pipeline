"""Slovak floor indexing (`floor`, `total_floors`) and property condition resolution."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.domain.reference import MAX_FLOOR, MODERN_COMPLEX_STEMS, normalize_text, parse_floor
from src.domain.sections import DescriptionSections


def is_ground_floor_text(text: str | None) -> bool:
    """Return True if the text states that the apartment itself is on the ground floor (`floor = 0`)."""
    if not text:
        return False
    cleaned = normalize_text(text)
    cleaned = re.sub(
        r"\b(?:pivnic\w*|kobk\w*|sklad\w*|garaz\w*|komor\w*)\s+[^.;,\n]{0,50}?\bna\s+(?:zvysenom\s+|vyvysenom\s+)?prizemi\b",
        " ",
        cleaned,
    )
    if re.search(
        r"\b(?:"
        r"(?:byt\w*|apartman\w*|garson\w*|nehnutelnost\w*)\s+(?:sa\s+nachadza\s+|je\s+(?:situovan\w+\s+)?|situovan\w+\s+)?na\s+(?:zvysenom\s+|vyvysenom\s+)?prizemi\b"
        r"|ktory\s+sa\s+nachadza\s+na\s+(?:zvysenom\s+|vyvysenom\s+)?prizemi\b"
        r"|(?:zvysene|vyvysene)\s+prizemie\b"
        r"|prizemn\w+\s+(?:[\w,.-]+\s+){0,4}(?:byt|apartman|garson\w*)\b"
        r"|(?:poschodie|podlazie|umiestnenie)\s*[:=-]?\s*(?:zvysene\s+|vyvysene\s+)?prizemie\b"
        r")",
        cleaned,
    ):
        return True

    return bool(
        re.search(r"\b(?:na\s+)?1\s*\.\s*(?:nadzemnom\s+podlazi|nadzemne\s+podlazie|np\b)", cleaned)
        and not re.search(r"\b[1-9]\d*\s*\.\s*poschod\w*", cleaned)
    )


def extract_floor_and_total_from_text(text: str | None) -> tuple[int | None, int | None]:
    """Extract `(floor, total_floors)` from Slovak title or description text."""
    if not text:
        return None, None

    cleaned = normalize_text(text)

    # 1. Explicit fraction patterns: "3/7 posch", "8/8 poschodie", "poschodie: 6/6", "p. 11/12", "4.p/5"
    m_frac = re.search(
        r"(?:\b(?:poschod\w*|podlaz\w*|p\.)\s*[:=-]?\s*(\d{1,2})\s*/\s*(\d{1,2})\b"
        r"|\b(\d{1,2})\s*/\s*(\d{1,2})\s*(?:poschod\w*|podlaz\w*|p\.)"
        r"|\b(\d{1,2})\s*\.?\s*(?:poschod\w*|podlaz\w*|p\.?)\s*/\s*(\d{1,2})\b)",
        cleaned,
    )
    if m_frac:
        fl = int(m_frac.group(1) or m_frac.group(3) or m_frac.group(5))
        tot = int(m_frac.group(2) or m_frac.group(4) or m_frac.group(6))
        if 0 <= fl <= MAX_FLOOR and 1 <= tot <= MAX_FLOOR and fl <= tot:
            if fl == 1 and is_ground_floor_text(text):
                fl = 0
            return fl, tot

    # 2. "na 3. poschodí zo 7", "poschodie 8. z 8.", "5. poschodie z celkových 8"
    m_of = re.search(
        r"\b(?:na\s+)?(\d{1,2})\s*\.?\s*(?:poschod\w*|podlaz\w*|np\b)\s+z(?:o)?\s+(?:celkov\w+\s+)?(\d{1,2})\b"
        r"|\bposchod\w*\s*[:=-]?\s*(\d{1,2})\s*\.?\s*z(?:o)?\s+(?:celkov\w+\s+)?(\d{1,2})\b",
        cleaned,
    )
    if m_of:
        fl = int(m_of.group(1) or m_of.group(3))
        tot = int(m_of.group(2) or m_of.group(4))
        if 0 <= fl <= MAX_FLOOR and 1 <= tot <= MAX_FLOOR and fl <= tot:
            if fl == 1 and is_ground_floor_text(text):
                fl = 0
            return fl, tot

    # 3. Ground floor ("prízemie", "zvýšené prízemie", "1. NP") vs single floor
    fl_val: int | None = None
    m_posch = re.search(
        r"\b(?:na\s+)?(?:najvyssom\s+)?(\d{1,2})\s*(?:\.\s*(?:posch\w*|p\b)|(?:\s+|\.)posch\w*)"
        r"|\bposchodie\s*[:=-]\s*(\d{1,2})\b",
        cleaned,
    )
    if m_posch:
        cand = int(m_posch.group(1) or m_posch.group(2))
        if 0 <= cand <= MAX_FLOOR:
            fl_val = cand
    elif is_ground_floor_text(text):
        fl_val = 0
    else:
        m_podl = re.search(
            r"\b(?:na\s+)?(\d{1,2})\s*(?:\.\s*(?:nadzemnom\s+)?(?:podlaz\w*|np\b)|(?:\s+|\.)(?:nadzemnom\s+)?(?:podlaz\w*|np\b))"
            r"|\bpodlazie\s*[:=-]\s*(\d{1,2})\b",
            cleaned,
        )
        if m_podl:
            cand = int(m_podl.group(1) or m_podl.group(2))
            if cand == 1:
                fl_val = 0
            elif 2 <= cand <= MAX_FLOOR:
                fl_val = cand

    # 4. Total floors in building
    tot_val: int | None = None
    m_tot = re.search(
        r"\b(?:celkovo|spolu|pozostava\s+zo?)\s+(\d{1,2})\s+(?:nadzemn\w+\s+)?(?:poschod\w*|podlaz\w*)\b"
        r"|\bna\s+(?:zvysenom\s+|vyvysenom\s+)?prizemi\s+(?:zatepleneho\s+)?(\d{1,2})\s*(?:-?\s*poschodov\w*|\s*podlazn\w*|p\.)",
        cleaned,
    )
    if m_tot:
        cand_tot = int(m_tot.group(1) or m_tot.group(2))
        if 2 <= cand_tot <= MAX_FLOOR and (fl_val is None or fl_val <= cand_tot):
            tot_val = cand_tot

    return fl_val, tot_val


def resolve_floor_and_total(
    raw_floor: Any,
    raw_total_floors: Any,
    umiestnenie: str | None,
    title: str | None,
    sections: DescriptionSections,
) -> tuple[int | None, int | None]:
    """Unified single-pass floor & total_floors resolution and Slovak 0-based indexing."""
    umiest_clean = normalize_text(umiestnenie)
    fl = parse_floor(raw_floor)
    tot = parse_floor(raw_total_floors)
    desc_scope = sections.non_location_text

    # Clear realtor `1/1` placeholder when description states a different floor
    if fl == 1 and tot == 1:
        tot = None
        t_fl_c, _ = extract_floor_and_total_from_text(title)
        d_fl_c, _ = extract_floor_and_total_from_text(desc_scope)
        if (t_fl_c is not None and t_fl_c != 1) or (d_fl_c is not None and d_fl_c != 1):
            fl = None

    if fl is None or tot is None:
        t_fl, t_tot = extract_floor_and_total_from_text(title)
        d_fl, d_tot = extract_floor_and_total_from_text(desc_scope)
        if fl is None:
            fl = t_fl if t_fl is not None else d_fl
        if tot is None:
            tot = t_tot if t_tot is not None else d_tot

    # Normalize ground-floor apartments (Prízemie / Zvýšené prízemie / 1. NP) to floor = 0
    if fl in (None, 1) and (
        "prizemie" in umiest_clean
        or is_ground_floor_text(title)
        or is_ground_floor_text(desc_scope)
    ):
        fl = 0

    # Cross-fill top-floor apartments ("Posledné podlažie") when one of floor / total_floors is known
    if "posledne podlazie" in umiest_clean:
        if fl is None and tot is not None and tot >= 1:
            fl = tot
        elif tot is None and fl is not None and fl >= 2:
            tot = fl

    # Top-floor NP -> poschodie reconciliation (guarded against elevator-stop phrases)
    if fl is not None and tot is not None and fl == tot and fl >= 2:
        target_posch = fl - 1
        combined_fl_text = f"{normalize_text(title)} {normalize_text(desc_scope)}"
        confirms_current_posch = bool(
            re.search(rf"\b{fl}\s*(?:\.\s*poschod\w*|/\s*{tot}\s*poschod\w*)", combined_fl_text)
        )
        fl_text_no_elev = re.sub(
            r"\bvytah\w*\s+(?:premava\s+|ide\s+|je\s+)?po\s+\d+\s*\.\s*poschod\w*",
            " ",
            combined_fl_text,
        )
        if not confirms_current_posch and re.search(
            rf"\b{target_posch}\s*\.\s*(?:nadzemnom\s+podlazi|poschod\w*|np\b)",
            fl_text_no_elev,
        ):
            fl = target_posch

    if tot is not None and (tot <= 1 or (fl is not None and fl > tot)):
        tot = None

    return fl, tot


def infer_property_condition(text: str | None) -> str | None:
    """Infer canonical Slovak property condition from title or apartment-section text."""
    if not text:
        return None
    cleaned = normalize_text(text)
    if "vo vystavbe" in cleaned:
        return "Vo výstavbe"
    if re.search(
        r"\b(?:kompletn\w+\s+rekonstrukci\w*|kompletne\s+zrekonstruovan\w*|po\s+kompletnej\s+rekonstrukcii)\b",
        cleaned,
    ):
        return "Kompletná rekonštrukcia"
    if re.search(r"\b(?:ciastocn\w+\s+rekonstrukci\w*|ciastocne\s+zrekonstruovan\w*)\b", cleaned):
        return "Čiastočná rekonštrukcia"
    if re.search(r"\b(?:povodn\w+\s+stav\w*)\b", cleaned):
        return "Pôvodný stav"
    if "novostavb" in cleaned:
        return "Novostavba"
    return None


def has_apartment_complete_reconstruction(text: str | None) -> bool:
    """Return True if text explicitly states the apartment underwent complete reconstruction."""
    if not text:
        return False
    cleaned = normalize_text(text)
    cleaned = re.sub(
        r"\b(?:kompletne\s+)?zrekonstruovan\w+\s+(?:a\s+zateplen\w+\s+|zateplen\w+\s+)?(?:bytov\w+\s+)?dom\b"
        r"|\b(?:bytov\w+\s+)?dom\s+(?:presiel\s+|je\s+po\s+)?(?:kompletnou\s+|kompletnej\s+)?rekonstrukci\w*",
        " ",
        cleaned,
    )
    return bool(
        re.search(
            r"(?:-\s*kompletna\s+rekonstrukcia\b"
            r"|\bbyt\s+(?:presiel|je\s+po)\s+kompletn\w+\s+rekonstrukci\w*"
            r"|\bkompletna\s+rekonstrukcia\s+bytu\b"
            r"|\bkompletne\s+zrekonstruovan\w+\s+(?:\d\s*-?\s*izb\w+\s+)?byt\b)",
            cleaned,
        )
    )


def _is_old_panel_building(
    construction: str | None,
    year_of_construction: int | None,
    year_of_construction_lookup: str | None,
) -> bool:
    """Return True if structured portal attributes prove this is an older/panel building."""
    constr_clean = normalize_text(construction)
    if "panelov" in constr_clean:
        return True
    if year_of_construction is not None and 1900 <= year_of_construction <= 2005:
        return True
    return bool(
        year_of_construction_lookup and re.search(r"(19\d{2})", year_of_construction_lookup)
    )


def is_modern_complex_title(title: str | None) -> bool:
    """Return True if the listing title names a known modern Slovak residential complex."""
    t_clean = normalize_text(title)
    if not t_clean:
        return False
    return any(re.search(rf"\b{re.escape(stem)}", t_clean) for stem in MODERN_COMPLEX_STEMS)


def resolve_property_condition(
    raw_condition: str | None,
    title: str | None,
    sections: DescriptionSections,
    construction: str | None = None,
    year_of_construction: int | None = None,
    year_of_construction_lookup: str | None = None,
    rok_kolaudacie: float | None = None,
    reference_year: int | None = None,
) -> str | None:
    """Resolve canonical Slovak property condition respecting structural building age/type."""
    apt_text = sections.apartment_text
    desc_clean = normalize_text(apt_text)
    title_cond = infer_property_condition(title)
    is_modern = is_modern_complex_title(title)
    is_old_or_panel = _is_old_panel_building(
        construction, year_of_construction, year_of_construction_lookup
    )
    ref_year = reference_year or datetime.now().year

    cond = raw_condition
    if not cond:
        inferred = title_cond or infer_property_condition(apt_text)
        if inferred == "Novostavba" and is_old_or_panel:
            inferred = None
        if not inferred and is_modern and not is_old_or_panel:
            inferred = "Novostavba"
        if (
            not inferred
            and rok_kolaudacie is not None
            and not is_old_or_panel
            and (ref_year - 6) <= rok_kolaudacie <= (ref_year + 4)
        ):
            inferred = "Novostavba"
        return inferred

    if cond == "Pôvodný stav":
        if (
            not is_old_or_panel
            and (title_cond in ("Novostavba", "Vo výstavbe") or is_modern)
            and "povodn" not in desc_clean
        ):
            return title_cond or "Novostavba"
        if has_apartment_complete_reconstruction(title) or has_apartment_complete_reconstruction(
            apt_text
        ):
            return "Kompletná rekonštrukcia"
    elif cond == "Čiastočná rekonštrukcia":
        if has_apartment_complete_reconstruction(title) or has_apartment_complete_reconstruction(
            apt_text
        ):
            return "Kompletná rekonštrukcia"
    elif cond == "Vo výstavbe":
        if title_cond in ("Čiastočná rekonštrukcia", "Kompletná rekonštrukcia", "Pôvodný stav"):
            return title_cond
        if re.search(r"\bbyt\s+presiel\s+ciastocn\w+\s+rekonstrukci\w*", desc_clean):
            return "Čiastočná rekonštrukcia"
    elif cond == "Novostavba":
        if re.search(r"\bstav\s*:\s*vo\s+vystavbe\b", desc_clean):
            return "Vo výstavbe"
        m_done = re.search(
            r"\b(?:ukoncenie\s+vystavby|planovane\s+dokoncenie)\s*[:=-]?\s*(?:[1-4]\.\s*(?:stvrtrok|kvartal)|q[1-4])?\s*(20\d{2})\b",
            desc_clean,
        )
        if m_done and int(m_done.group(1)) >= ref_year + 1:
            return "Vo výstavbe"

    return cond
