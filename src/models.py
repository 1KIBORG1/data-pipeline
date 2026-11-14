"""Pydantic v2 data validation and normalization model for apartment listings.

Applies field-level parsing (numbers, floors, room counts, districts) followed
by cross-field domain consistency contracts:
1. Reference Data Whitelist (`SLOVAK_DISTRICTS` — all 79 official Slovak districts).
2. Entity Classification Gate (rejecting reserved/sold/wanted/foreign/commercial/house ads).
3. Source Trust Hierarchy (`category_sub` vs. free-text `title` room count resolution).
4. Three-Valued Logic (`has_balcony: bool | None` — `True`, `False`, or `None` when unspecified)
   with morphological stem matching (`balkon`, `loggi`, `lodzi`, `teras`).
5. Cross-Field Physical Plausibility (`area_sqm` vs. `rooms_count`, unit price bounds).
"""

import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

# Valid building floor range in Slovakia (-2 = underground levels, 46 = Eurovea Tower)
MIN_FLOOR = -2
MAX_FLOOR = 46

# Official 79 districts (okresy) of the Slovak Republic + city-level fallbacks
SLOVAK_DISTRICTS: frozenset[str] = frozenset({
    "Bánovce nad Bebravou",
    "Banská Bystrica",
    "Banská Štiavnica",
    "Bardejov",
    "Bratislava",
    "Bratislava I",
    "Bratislava II",
    "Bratislava III",
    "Bratislava IV",
    "Bratislava V",
    "Brezno",
    "Bytča",
    "Čadca",
    "Detva",
    "Dolný Kubín",
    "Dunajská Streda",
    "Galanta",
    "Gelnica",
    "Hlohovec",
    "Humenné",
    "Ilava",
    "Kežmarok",
    "Komárno",
    "Košice",
    "Košice I",
    "Košice II",
    "Košice III",
    "Košice IV",
    "Košice-okolie",
    "Krupina",
    "Kysucké Nové Mesto",
    "Levice",
    "Levoča",
    "Liptovský Mikuláš",
    "Lučenec",
    "Malacky",
    "Martin",
    "Medzilaborce",
    "Michalovce",
    "Myjava",
    "Námestovo",
    "Nitra",
    "Nové Mesto nad Váhom",
    "Nové Zámky",
    "Partizánske",
    "Pezinok",
    "Piešťany",
    "Poltár",
    "Poprad",
    "Považská Bystrica",
    "Prešov",
    "Prievidza",
    "Púchov",
    "Revúca",
    "Rimavská Sobota",
    "Rožňava",
    "Ružomberok",
    "Sabinov",
    "Šaľa",
    "Senec",
    "Senica",
    "Skalica",
    "Snina",
    "Sobrance",
    "Spišská Nová Ves",
    "Stará Ľubovňa",
    "Stropkov",
    "Svidník",
    "Topoľčany",
    "Trebišov",
    "Trenčín",
    "Trnava",
    "Turčianske Teplice",
    "Tvrdošín",
    "Veľký Krtíš",
    "Vranov nad Topľou",
    "Žarnovica",
    "Žiar nad Hronom",
    "Žilina",
    "Zlaté Moravce",
    "Zvolen",
})

# Canonical room count mapping by Nehnutelnosti.sk apartment subcategory code
ROOMS_BY_SUBCATEGORY: dict[str, int] = {
    "GARSONKA": 1,
    "STUDIO_APARTMENT": 1,
    "DOUBLE_GARSONKA": 2,
    "DOUBLE_STUDIO_APARTMENT": 2,
    "ONE_ROOM_APARTMENT": 1,
    "TWO_ROOM_APARTMENT": 2,
    "THREE_ROOM_APARTMENT": 3,
    "FOUR_ROOM_APARTMENT": 4,
    "FIVE_AND_MORE_ROOM_APARTMENT": 5,
    "FIVE_PLUS_ROOM_APARTMENT": 5,
}

# Minimum physically plausible floor area (m2) by room count
MIN_AREA_BY_ROOMS: dict[int, float] = {
    1: 10.0,
    2: 20.0,
    3: 30.0,
    4: 42.0,
    5: 55.0,
}

# Residential apartment keywords used in entity classification
APARTMENT_KEYWORDS = (
    "byt",
    "apartman",
    "garson",
    "garzon",
    "mezonet",
    "loft",
    "studio",
    "penthouse",
    "prazak",
    "bauring",
)

# Diacritic-stripped title stems that indicate inactive, wanted, foreign, or non-apartment ads
REJECT_TITLE_STEMS = (
    "rezervovan",              # Reserved listings ("rezervované", "rezervovaný")
    "rezervovn",               # Common agency typo ("rezervovné")
    "rezervvov",               # Common agency typo ("rezervvované")
    "rezervovali",             # Reserved listings ("rezervovali sme")
    "rezervaci",               # Reserved listings ("rezervácia")
    "rezerve",                 # Reserved listings ("rezervé")
    "reserve",                 # French/English "RESERVÉ" / "RESERVED" tag in titles
    "predan",                  # Already sold listings
    "prenajat",                # Already rented listings
    "kupim",                   # Buyer wanted ads ("Kúpim byt")
    "hladam",                  # Buyer wanted ads ("Hľadám byt")
    "dopyt",                   # Demand/inquiry ads
    "vymen byt",               # House-for-apartment swap promotions
    "vymena bytu",             # Apartment swap ads ("Výmena bytu")
    "samostatna izba",         # Single shared-room rentals
    "samostatne izby",         # Shared rooms
    "spolubyvani",             # Flatshare ads
    "slobodark",               # Dormitory / lodging-house room ("slobodárka")
    "odstupeni",               # Lease/mortgage transfer ads ("Odstúpenie")
    "drazb",                   # Auctions / foreclosures ("Dražba")
    "vykup nehnutelnost",      # Property buyout promotions
    "polovicny podiel",        # Partial 1/2 ownership shares
    "spoluvlastnicky podiel",  # Fractional co-ownership shares
    "penzion",                 # Commercial guest houses
    "apartmanovy dom",         # Entire apartment buildings sold as a whole
    "apartmanoveho domu",      # Entire apartment buildings sold as a whole (genitive)
    "investicny areal",        # Multi-building commercial/residential complexes
    "prerobena budova",        # Whole commercial/residential buildings
    "investicna budova",       # Whole commercial/investment buildings
    "samostatna budova",       # Whole standalone buildings
    "apartmanov + 2 domy",     # Multi-unit complex bundles
    "mobilny dom",             # Mobile homes on land plots
    "bungalov",                # Single-story family houses ("bungalov")
    "horska chata",            # Mountain cottages
    "podkrovny priestor",      # Unconverted raw attic spaces
    "predaj garaze",           # Standalone garage sale ads
    "vodarenska studna",       # Industrial water well ads
    "testovaci inzerat",       # Portal internal QA test listings
    "test inzerat",            # Portal internal QA test listings
    "123 test",                # Portal internal QA test listings
    "skuska nova",             # Portal internal QA test listings
    "autofill addon",          # Portal internal QA test listings
    "koja a parkovacie",       # Storage + parking bundles without an apartment
    "na dlhodoby prenajom",    # Long-term rentals misclassified under sale
    "prenajom - dlhodobo",     # Long-term rentals misclassified under sale
    "dam do prenajmu",         # Rental offers misclassified under sale
    "ponukame na prenajom",    # Rental offers misclassified under sale
    "chorvatsk",               # Foreign resort ads (Croatia)
    "ostrova ciovo",           # Foreign resort ads (Croatia)
    "ostrove ciovo",           # Foreign resort ads (Croatia)
    "v rakusk",                # Foreign property ads (Austria)
    "wolfsthal",               # Foreign border village ads (Wolfsthal, Austria)
    "gruzinsk",                # Foreign property ads (Georgia)
    "tbilisi",                 # Foreign property ads (Tbilisi, Georgia)
    "dominikansk",             # Foreign resort ads (Dominican Republic)
    "pri mori",                # Foreign seaside property ads
    "pri ciernom mori",        # Foreign seaside property ads (Bulgaria)
    "sunset resort pomorie",   # Foreign seaside property ads (Bulgaria)
    "more na dosah",           # Foreign seaside property ads (Turkey)
    "hurgad",                  # Foreign resort ads (Egypt)
    "hurghad",                 # Foreign resort ads (Egypt)
    "na bali",                 # Foreign resort ads (Indonesia)
    "maldiv",                  # Foreign resort ads (Maldives)
    "thulusdhoo",              # Foreign resort ads (Maldives)
    "aguas nuevas",            # Foreign resort ads (Spain)
    "v spanielsku",            # Foreign resort ads (Spain)
    "v bulharsku",             # Foreign resort ads (Bulgaria)
    "v taliansku",             # Foreign resort ads (Italy)
    "v dubaji",                # Foreign resort ads (UAE)
)

# Slovak numeral stems used in listing titles when room count is written as a word
WORD_ROOMS_MAP: tuple[tuple[str, int], ...] = (
    ("dvojgarson", 2),
    ("dvojgarzon", 2),
    ("jednoizb", 1),
    ("dvojizb", 2),
    ("trojizb", 3),
    ("stvorizb", 4),
    ("patizb", 5),
    ("sestizb", 6),
    ("sedemizb", 7),
    ("garson", 1),
    ("garzon", 1),
    ("studio", 1),
    ("1kk", 1),
)


def _normalize_dashes(text: str) -> str:
    """Normalize Unicode hyphens and dashes (`–`, `—`, `‐`, `‑`, `−`) to ASCII `-`."""
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text)


def _parse_room_token(text: str) -> int | None:
    """Parse a single room-count token from normalized text."""
    # Check 2-garsonka / dvojgarsonka before generic 1-room garsonka.
    # Disallow a preceding letter so NFKD-decomposed "28 m² garsónka" ("28 m2 garsonka")
    # does not match "2 garsonka" at the end of "m2".
    if re.search(r"(?<!\d[.,])(?<![\dA-Za-z])(?:2|dvoj)\s*-?\s*gar[sz]on", text):
        return 2

    # Only .5 / ,5 is a valid fractional half-room in Slovakia (e.g. "1,5-izbový", "2,5i")
    match_rooms = re.search(
        r"(?<![.,\d])([1-9])(?:[.,]5\.?|\.)?(?:\s*(?:-\s*)?(?:izb|izieb)|\s*-\s*i\b|i\b|\s*\+\s*(?:kk|k|1)\b)",
        text,
    )
    if match_rooms:
        return int(match_rooms.group(1))

    # Avoid overriding multi-component "apartmán + štúdio" with 1-room "studio"
    if "apartman" in text and "studio" in text:
        return None

    for stem, count in WORD_ROOMS_MAP:
        if stem in text:
            return count

    return None


def extract_rooms_from_title(title_clean: str) -> int | None:
    """Extract room count from a normalized (lowercase, no-diacritics) listing title.

    Implements Source Trust Hierarchy: returns `None` when the title is a multi-unit
    developer range/list (e.g. `'1, 2 a 3 izbové byty'`, `'1- až 4-izbové'`, `'2-3 izbový'`)
    so the structured `category_sub` attribute is preserved.
    """
    tc = _normalize_dashes(title_clean)

    # 1. Strip hypothetical conversion clauses ("možnosť prerobenia na 4-izbový", "flexibilná dispozícia až pre 5 izieb")
    has_hypothetical = bool(
        re.search(
            r"\b(?:(?:s\s+)?moznost\w*\s+(?:prerob\w*|uprav\w*|zmen\w*|premen\w*|rozdel\w*|vytvor\w*|dokoncen\w*|[1-9])"
            r"|potencial\w*\s+(?:prerob\w*|uprav\w*|zmen\w*|[1-9])"
            r"|flexibiln\w+\s+dispozici\w*\s+az\s+pre)\b",
            tc,
        )
    )
    if has_hypothetical:
        tc = re.sub(
            r"\b(?:(?:s\s+)?moznost\w*\s+(?:prerob\w*|uprav\w*|zmen\w*|premen\w*|rozdel\w*|vytvor\w*|dokoncen\w*|[1-9])"
            r"|potencial\w*\s+(?:prerob\w*|uprav\w*|zmen\w*|[1-9])"
            r"|flexibiln\w+\s+dispozici\w*\s+az\s+pre)\s*[^,;|()]*",
            " ",
            tc,
        )

    # 2. Normalize half-room decimals ("1,5" -> "1.5", "1 a 1/2" -> "1.5") before range detection
    tc_half_safe = re.sub(r"\b([1-9])\s+a\s+1/2\b", r"\1.5", tc)
    tc_half_safe = re.sub(r"(?<=\d),(?=5(?:$|[^0-9]))", ".", tc_half_safe)

    # 3. Defer to structured category_sub on multi-unit developer ranges ("1, 2 a 3 izbové", "2-3 izbový")
    m_range = re.search(
        r"\b([1-9])\s*-?\s*(?:(?:,\s*|\ba\b|\baz\b|\balebo\b|/|-)\s*([1-9])\s*-?\s*)+(?:izb|i\b)",
        tc_half_safe,
    )
    if m_range and m_range.group(1) != m_range.group(2):
        return None

    distinct_rooms = set(re.findall(r"(?<![.,\d])([1-9])(?:\s*-?\s*izb)", tc_half_safe))
    if len(distinct_rooms) >= 2 and re.search(r"\b(?:byty|apartmany)\b", tc_half_safe):
        return None

    # 4. Completed apartment conversions ("2-izbový prerobený na 3-izbový" or "upravený 2-izbový byt na 3-izbový")
    if not has_hypothetical:
        rem = re.search(
            r"\b(?:preroben\w*|upraven\w*|zmenen\w*|vyuzivan\w*)\s+(?:z\s+\S+(?:\s+izb\w*)?\s+)?(?:[1-9]\s*-?\s*izb\w*\s+(?:byt\w*\s+)?)?(?:na|ako)\s+"
            r"([1-9]\s*(?:-\s*)?(?:izb\w*|i\b)|(?:jedno|dvoj|troj|stvor|pat)izb\w*|[1-9](?=\s*(?:$|[|,/-])))",
            tc_half_safe,
        )
        if rem:
            target = rem.group(1).strip()
            if target.isdigit():
                return int(target)
            converted = _parse_room_token(target)
            if converted is not None:
                return converted

        # Combined dual-apartment listings ("Spojený 3 a 1i byt")
        m_combined = re.search(
            r"\bspojen\w+\s+([1-9])\s+a\s+([1-9])\s*-?\s*(?:izb\w*|i\b)",
            tc_half_safe,
        )
        if m_combined:
            return int(m_combined.group(1)) + int(m_combined.group(2))

    return _parse_room_token(tc_half_safe)


def parse_positive_float(value: Any) -> float | None:
    """Parse messy numeric strings (e.g. `'2 043,80 €/m²'`) into a positive float."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return round(float(value), 2) if value > 0 else None

    if isinstance(value, str):
        value = value.replace("\xa0", " ")
        # Remove thousands-separator spaces between digits ("2 043" -> "2043")
        value = re.sub(r"(?<=\d)\s+(?=\d)", "", value)
        value = value.replace(",", ".")

        match = re.search(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value)
        if match is None:
            return None

        number = float(match.group())
        return round(number, 2) if number > 0 else None

    return None


def parse_floor(value: Any) -> int | None:
    """Normalize Slovak floor descriptions (`'Prízemie'` -> 0, `'Suterén'` -> -1) to int."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, str):
        text = value.lower()
        if "prízemie" in text or "prizemie" in text:
            return 0
        if "suterén" in text or "suteren" in text:
            return -1

        match = re.search(r"-?\d+", text)
        if match is None:
            return None
        number = int(match.group())
    elif isinstance(value, int):
        number = value
    else:
        return None

    return number if MIN_FLOOR <= number <= MAX_FLOOR else None


def parse_rooms_count(value: Any) -> int | None:
    """Parse raw room count attribute into an integer."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value) if value.is_integer() else None

    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match is not None else None

    return None


def clean_district(value: Any) -> str | None:
    """Strip the redundant `'okres '` prefix and normalize hyphen spacing in district names."""
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    value = re.sub(r"^okres\s+", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s*-\s*", "-", value)


def _remove_diacritics(value: str) -> str:
    """Strip accents/diacritics from Unicode text using NFKD decomposition."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


# Negated balcony/loggia/terrace expressions in Slovak ("bez balkóna", "nemá balkón", "balkón: nie")
NEGATED_BALCONY_RE = re.compile(
    r"\b(?:"
    r"bez\s+(?:\w+\s+){0,2}(?:balkon|loggi|lodzi|teras)\w*"
    r"|(?:nema|nemaju|nedisponuje|neobsahuje|chyba)\s+(?:\w+\s+){0,3}(?:balkon|loggi|lodzi|teras)\w*"
    r"|(?:balkon|loggi\w*|lodzi\w*|teras\w*)\s*(?:[:=-]|\s+je)?\s*(?:nie|0\b|ziadn\w*|nema)"
    r"|nie\s+je\s+(?:k\s+dispozicii\s+)?(?:balkon|loggi|lodzi|teras)\w*"
    r")\b"
)

# Košice - Terasa borough references and commercial cafe/restaurant terrace phrases
KOSICE_TERASA_RE = re.compile(
    r"\b(?:"
    r"(?:kosic\w*(?:\s+ii)?|ke|zapad|sidlisk\w*|lokalit\w*|cast\w*|mestsk\w+\s+cast\w*|oc|obchodn\w+\s+centr\w*|poliklinik\w*|blizk\w*|nedalek\w*|obchvat\w*|centrum|ul\.?|ulic\w*)\s*[-–,/]?\s*(?:na\s+)?teras[eaouy]"
    r"|teras[eaouy]\s*[-–,/]?\s*(?:v\s+)?kosic\w*"
    r"|(?:byt\w*|byvani\w*)\s+na\s+terase"
    r"|na\s+terase\s*[-–,/]?\s*(?:v\s+)?(?:kosic\w*|popradsk\w*|kezmarsk\w*|trieda\b)"
    r"|letn\w+\s+teras\w*"
    r"|teras\w*\s+(?:restauraci\w*|kaviarn\w*)"
    r")\b"
)

# Negated elevator expressions in Slovak ("bez výťahu", "nemá výťah", "výťah: nie")
NEGATED_ELEVATOR_RE = re.compile(
    r"\b(?:"
    r"bez\s+(?:\w+\s+){0,2}vytah\w*"
    r"|(?:nema|nemaju|nedisponuje|neobsahuje|chyba)\s+(?:\w+\s+){0,2}vytah\w*"
    r"|vytah\w*\s*(?:[:=-]|\s+je|\s+sa)?\s*(?:nie|0\b|ziadn\w*|nema|nenachadza)"
    r"|nie\s+je\s+(?:k\s+dispozicii\s+)?vytah\w*"
    r")\b"
)


def has_negated_balcony(text: str | None) -> bool:
    """Return True if the text explicitly states the absence of a balcony/loggia/terrace."""
    if not text:
        return False
    cleaned = _remove_diacritics(text).casefold()
    return bool(NEGATED_BALCONY_RE.search(cleaned))


def has_balcony_from_text(text: str | None) -> bool:
    """Detect non-negated balcony, loggia/lodžia, terrace, or winter garden stems in free text.

    Uses invariant morphological root stems (`balkon`, `loggi`, `lodzi`, `teras`)
    after masking explicit negations and `Košice - Terasa` neighborhood references.
    """
    if not text:
        return False

    cleaned = _remove_diacritics(text).casefold()
    if cleaned.startswith(("http://", "https://", "/detail/")):
        cleaned = cleaned.replace("-", " ")
    cleaned = NEGATED_BALCONY_RE.sub(" ", cleaned)
    cleaned = KOSICE_TERASA_RE.sub(" ", cleaned)

    return bool(
        re.search(
            r"\b(?:[1-9]\s*x\s*)?(?:balkon|loggi|lodzi|teras)\w*\b|\bzimn\w+\s+zahrad\w*\b",
            cleaned,
        )
    )


def has_negated_elevator(text: str | None) -> bool:
    """Return True if the text explicitly states the absence of an elevator ('bez výťahu')."""
    if not text:
        return False
    cleaned = _remove_diacritics(text).casefold()
    return bool(NEGATED_ELEVATOR_RE.search(cleaned))


def has_elevator_from_text(text: str | None) -> bool:
    """Detect non-negated elevator ('výťah') mentions in free text."""
    if not text:
        return False
    cleaned = _remove_diacritics(text).casefold()
    cleaned = NEGATED_ELEVATOR_RE.sub(" ", cleaned)
    return bool(re.search(r"\bvytah\w*\b", cleaned))


def extract_floor_and_total_from_text(text: str | None) -> tuple[int | None, int | None]:
    """Extract `(floor, total_floors)` from Slovak title or description text."""
    if not text:
        return None, None

    cleaned = _normalize_dashes(_remove_diacritics(text).casefold())

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
            return fl, tot

    # 3. Single floor: "na 4. poschodí", "1.posch.", "20 posch.", "na 5 poschodí", "poschodie: 3."
    fl_val: int | None = None
    m_single = re.search(
        r"\b(?:na\s+)?(\d{1,2})\s*(?:\.\s*(?:nadzemnom\s+)?(?:posch\w*|podlaz\w*|p\b|np\b)|(?:\s+|\.)(?:nadzemnom\s+)?(?:posch\w*|podlaz\w*|np\b))"
        r"|\b(?:poschodie|podlazie)\s*[:=-]\s*(\d{1,2})\b",
        cleaned,
    )
    if m_single:
        cand = int(m_single.group(1) or m_single.group(2))
        if 0 <= cand <= MAX_FLOOR:
            fl_val = cand
    elif re.search(
        r"\b(?:byt|apartman|garson\w*)\s+(?:sa\s+nachadza\s+|je\s+|situovan\w+\s+)?na\s+(?:zvysenom\s+)?prizemi\b"
        r"|\bprizemn\w+\s+(?:[\w,.-]+\s+){0,4}(?:byt|apartman|garson\w*)\b"
        r"|\b(?:poschodie|podlazie)\s*[:=-]?\s*(?:zvysene\s+)?prizemie\b",
        cleaned,
    ):
        fl_val = 0

    # 4. Total floors in building: "má celkovo 9 nadzemných podlaží", "z 8 poschodí"
    tot_val: int | None = None
    m_tot = re.search(
        r"\b(?:celkovo|spolu|pozostava\s+zo?)\s+(\d{1,2})\s+(?:nadzemn\w+\s+)?(?:poschod\w*|podlaz\w*)\b",
        cleaned,
    )
    if m_tot:
        cand_tot = int(m_tot.group(1))
        if 2 <= cand_tot <= MAX_FLOOR and (fl_val is None or fl_val <= cand_tot):
            tot_val = cand_tot

    return fl_val, tot_val


def extract_area_from_text(text: str | None, is_title: bool = False) -> float | None:
    """Extract apartment floor area (m2) from title or description when missing or invalid."""
    if not text:
        return None

    cleaned = _normalize_dashes(_remove_diacritics(text).casefold())

    # 1. Explicit labeled apartment area in description or title
    m_labeled = re.search(
        r"(?:"
        r"\b(?:vymera|rozloha|uzitkova\s+plocha|podlahova\s+plocha)\s+(?:bytu|apartmanu|garsonky|mezonetu)\s*(?:je|ma|[:=-])?\s*(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\b(?:byt|apartman|garsonka|mezonet)\s+(?:ma\s+(?:vymeru|rozlohu)|o\s+vymere|s\s+vymerou|o\s+rozlohe|s\s+rozlohou|s\s+uzitkovou\s+plochou)\s*(?:cca\s*)?(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r"|\b(?:vymera|uzitkova\s+plocha|podlahova\s+plocha)\s*[:=-]\s*(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)"
        r")",
        cleaned,
    )
    if m_labeled:
        raw_num = m_labeled.group(1) or m_labeled.group(2) or m_labeled.group(3)
        val = float(raw_num.replace(",", "."))
        if 12.0 <= val <= 400.0:
            return round(val, 2)

    # 2. In titles, match standalone `<N> m2` not attached to garden/terrace/balcony/garage/cellar
    if is_title:
        ext_words = (
            "pozem",
            "zahrad",
            "predzahrad",
            "teras",
            "balkon",
            "loggi",
            "lodzi",
            "garaz",
            "pivnic",
            "exterier",
            "ulozn",
            "poval",
            "nebytov",
        )
        for m in re.finditer(r"(?<![.,\d])(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²)\b", cleaned):
            val = float(m.group(1).replace(",", "."))
            prefix = re.split(r"[\d+|,/]", cleaned[max(0, m.start() - 25) : m.start()])[-1]
            # Stop suffix scan if another preposition ("s", "so", "+") introduces a separate feature
            suffix_raw = cleaned[m.end() : min(len(cleaned), m.end() + 20)]
            suffix = re.split(r"[\d+|,/]|\b(?:s|so|a)\s+", suffix_raw)[0]
            ctx = f"{prefix} {suffix}"
            if any(w in ctx for w in ext_words):
                continue
            if 12.0 <= val <= 400.0:
                return round(val, 2)

    return None


def infer_property_condition(text: str | None) -> str | None:
    """Infer canonical Slovak property condition from title or description when missing."""
    if not text:
        return None
    cleaned = _remove_diacritics(text).casefold()
    if "vo vystavbe" in cleaned:
        return "Vo výstavbe"
    if "novostavb" in cleaned:
        return "Novostavba"
    if re.search(r"\b(?:kompletn\w+\s+rekonstrukci\w*|kompletne\s+zrekonstruovan\w*|po\s+kompletnej\s+rekonstrukcii)\b", cleaned):
        return "Kompletná rekonštrukcia"
    if re.search(r"\b(?:ciastocn\w+\s+rekonstrukci\w*|ciastocne\s+zrekonstruovan\w*)\b", cleaned):
        return "Čiastočná rekonštrukcia"
    if re.search(r"\b(?:povodn\w+\s+stav\w*)\b", cleaned):
        return "Pôvodný stav"
    return None


class ApartmentModel(BaseModel):
    """Validated and normalized representation of a Slovak apartment listing."""

    id: str
    url: str
    title: str | None = None
    transaction_type: str | None = None
    category_sub: str | None = None
    rooms_count: int | None = None
    price_eur: int | None = None
    price_per_sqm_eur: float | None = None
    area_sqm: float | None = None
    floor: int | None = None
    total_floors: int | None = None
    property_condition: str | None = None
    has_elevator: bool = False
    has_balcony: bool | None = None
    city: str | None = None
    district: str | None = None
    updated_at: datetime | None = None

    @field_validator("transaction_type", mode="before")
    @classmethod
    def normalize_transaction_type(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _remove_diacritics(value.strip()).casefold() or None

    @field_validator("price_eur", mode="before")
    @classmethod
    def normalize_price_eur(cls, value: Any) -> int | None:
        parsed = parse_positive_float(value)
        return round(parsed) if parsed is not None else None

    @field_validator("area_sqm", "price_per_sqm_eur", mode="before")
    @classmethod
    def normalize_positive_measurements(cls, value: Any) -> float | None:
        return parse_positive_float(value)

    @field_validator("floor", "total_floors", mode="before")
    @classmethod
    def normalize_floors(cls, value: Any) -> int | None:
        return parse_floor(value)

    @field_validator("rooms_count", mode="before")
    @classmethod
    def normalize_rooms_count(cls, value: Any) -> int | None:
        return parse_rooms_count(value)

    @field_validator("district", mode="before")
    @classmethod
    def normalize_district(cls, value: Any) -> str | None:
        return clean_district(value)

    @field_validator("category_sub", mode="before")
    @classmethod
    def normalize_category_sub(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @field_validator("title", "property_condition", "city", mode="before")
    @classmethod
    def clean_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip() or None
        return value

    @model_validator(mode="after")
    def validate_apartment_consistency(self) -> "ApartmentModel":
        """Enforce cross-field domain contracts, resolve room counts, and validate prices."""
        title_clean = (
            _normalize_dashes(_remove_diacritics(self.title).casefold())
            if self.title
            else ""
        )

        # 1. Entity Classification & Inactive/Foreign Listing Contract
        if title_clean:
            rejected_word = next(
                (stem for stem in REJECT_TITLE_STEMS if stem in title_clean),
                None,
            )
            if rejected_word is not None:
                raise ValueError(
                    f"Apartment title contains a rejected stem: {rejected_word}"
                )

            if title_clean.startswith(("na prenajom", "prenajom ", "parkovacie miest")):
                raise ValueError("Apartment title indicates a rental or standalone parking listing")

            # Reject pure rental or swap ads that have no sale price
            if self.price_eur is None:
                tc_no_inv = re.sub(
                    r"\b(?:vhodn\w*|idealn\w*)\s+(?:aj\s+)?na\s+(?:dlhodob\w*\s+(?:i\s+|aj\s+)?|kratkodob\w*\s+)*prenajom\b",
                    "",
                    title_clean,
                )
                if (
                    re.search(r"\b(?:na\s+prenajom|prenajom|prenajmeme|vymena)\b", tc_no_inv)
                    and "predaj" not in tc_no_inv
                    and "predam" not in tc_no_inv
                ):
                    raise ValueError("Listing is a rental or swap without a sale price")

            # Reject commercial non-residential spaces lacking residential apartment designation
            if re.search(r"\b(?:nebytov\w*\s+priestor\w*|obchodn\w*\s+priestor\w*)\b", title_clean):
                tc_no_hyp = re.sub(
                    r"\bmoznost\w*\s+(?:premeny|prerobenia|zmeny)\s+na\s+[^,.;]+",
                    "",
                    title_clean,
                )
                if (
                    not re.search(r"\b(?:byt\w*|holobyt\w*|apartman\w*|garson\w*|garzon\w*|loft\w*|studi\w*)\b", tc_no_hyp)
                    or "kancelaria" in tc_no_hyp
                ):
                    raise ValueError("Listing title indicates a commercial non-residential space")

            # Reject whole multi-unit residential buildings sold as a bundle
            if re.search(
                r"\b(?:bytov\w+\s+dom\s+s\s+\d+|predaj\s+bytoveho\s+domu|trojpodlazn\w+\s+bytov\w+\s+dom"
                r"|vila\s+s\s+\d+\s+byt\w*|(?:rodinny\s+)?dom\s+s\s+(?:\d+|dvomi|dvoma|tromi)\s+bytov\w*\s+jednotk\w*"
                r"|(?:dva|tri|styri)\s+(?:\d\s*-?\s*izb\w*\s*)?byty\s+v\s+(?:1\s+|jednom\s+)?(?:rodinnom|tehlovom)\s+dome)\b",
                title_clean,
            ):
                raise ValueError("Listing title indicates an entire multi-unit building")

            # Reject family houses, cottages, and commercial buildings when no apartment keyword is present
            has_apt_kw = any(kw in title_clean for kw in APARTMENT_KEYWORDS)
            if not has_apt_kw:
                tc_no_loc = title_clean.replace("zochova chata", "")
                if re.search(
                    r"\b(?:(?:rodinny|rodinneho|dvojgeneracny|poschodovy|samostatny)\s+dom(?:u|e|y|om|och|ami)?\b"
                    r"|rodinnych\s+domov\b"
                    r"|\d+\s*-?\s*izb\w*\s+(?:rodinny\s+)?dom\b|\d+i\s+domov|dom\s+s\s+hotovym"
                    r"|domu\s*/\s*chalupy|dom\s*/\s*vila|rodinna\s+vila|chata\b|chaty\b|chatu\b|chate\b|chalup\w*"
                    r"|podnikatelsk\w+\s+objekt\w*|budov\w+\s+na\s+podnikanie|polyfunkcn\w+\s+objekt\w*"
                    r"|administrativn\w+\s+(?:budov\w*|objekt\w*))\b",
                    tc_no_loc,
                ):
                    raise ValueError("Listing title indicates a house or commercial building rather than an apartment")

        # 2. Geographic Scope Contract: require Slovak location and validate against official 79 districts
        if self.city and self.city.casefold() == "slovensko":
            self.city = None
        if not self.city and self.district:
            if self.district.startswith("Bratislava"):
                self.city = "Bratislava"
            elif self.district.startswith("Košice"):
                self.city = "Košice"
            else:
                self.city = self.district
        if not self.district and self.city:
            if self.city.startswith("Bratislava"):
                self.district = "Bratislava"
            elif self.city.startswith("Košice"):
                self.district = "Košice"

        if not self.city and not self.district:
            raise ValueError("Listing has neither city nor district in Slovakia")
        if self.district is not None and self.district not in SLOVAK_DISTRICTS:
            raise ValueError(f"District is outside the 79 official districts of Slovakia: {self.district}")

        # 3. Recover floor and total_floors from title if missing
        if self.floor is None or self.total_floors is None:
            t_fl, t_tot = extract_floor_and_total_from_text(self.title)
            if self.floor is None and t_fl is not None:
                self.floor = t_fl
            if self.total_floors is None and t_tot is not None:
                self.total_floors = t_tot

        # 4. Three-valued balcony resolution (`True` = confirmed, `False` = negated, `None` = unspecified)
        if self.has_balcony is not True:
            if not has_negated_balcony(self.title) and (
                has_balcony_from_text(self.title) or has_balcony_from_text(self.url)
            ):
                self.has_balcony = True
            elif self.has_balcony is None and has_negated_balcony(self.title):
                self.has_balcony = False

        if not self.has_elevator and not has_negated_elevator(self.title):
            if (
                has_elevator_from_text(self.title)
                or (self.floor is not None and self.floor >= 6)
                or (self.total_floors is not None and self.total_floors >= 7)
            ):
                self.has_elevator = True

        # 5. Source Trust Hierarchy for room count:
        # Unambiguous single-unit title -> structured category_sub -> validated raw attribute
        raw_rooms = (
            self.rooms_count
            if (self.rooms_count is not None and 1 <= self.rooms_count <= 10)
            else None
        )
        sub_rooms = ROOMS_BY_SUBCATEGORY.get(self.category_sub or "")
        title_rooms = extract_rooms_from_title(title_clean) if title_clean else None

        if title_rooms is not None:
            self.rooms_count = title_rooms
        elif sub_rooms is not None:
            if self.category_sub in ("FIVE_AND_MORE_ROOM_APARTMENT", "FIVE_PLUS_ROOM_APARTMENT"):
                self.rooms_count = raw_rooms if (raw_rooms is not None and raw_rooms >= 5) else 5
            else:
                self.rooms_count = sub_rooms
        else:
            if (
                raw_rooms is not None
                and raw_rooms >= 4
                and self.area_sqm is not None
                and self.area_sqm < MIN_AREA_BY_ROOMS.get(raw_rooms, 42.0)
            ):
                self.rooms_count = None
            else:
                self.rooms_count = raw_rooms

        # 6. Cross-Field Physical Plausibility Contract for floor area (area_sqm vs. rooms_count)
        min_area = MIN_AREA_BY_ROOMS.get(self.rooms_count or 1, 10.0)
        if self.area_sqm is not None and not (min_area <= self.area_sqm <= 600.0):
            self.area_sqm = None
            self.price_per_sqm_eur = None

        # Correct missing decimal point typos on 1-2 room apartments (e.g. 392.0 -> 39.2 m2, 514.0 -> 51.4 m2)
        if (
            self.area_sqm is not None
            and self.rooms_count in (1, 2)
            and self.area_sqm > 200.0
            and int(self.area_sqm) % 10 != 0
        ):
            self.area_sqm = round(self.area_sqm / 10.0, 2)

        # Recover or correct area_sqm when it mistakenly sums a garden/terrace/plot with apartment m2
        title_area = extract_area_from_text(self.title, is_title=True)
        if self.area_sqm is None and title_area is not None and title_area >= min_area:
            self.area_sqm = title_area
        elif (
            self.area_sqm is not None
            and title_area is not None
            and title_area >= min_area
            and self.area_sqm > max(title_area * 1.45, title_area + 35.0)
        ):
            self.area_sqm = title_area

        # 7. Resolve property_condition from title when missing or when default 'Pôvodný stav' contradicts 'Novostavba'
        title_cond = infer_property_condition(self.title) if self.title else None
        if not self.property_condition:
            self.property_condition = title_cond
        elif self.property_condition == "Pôvodný stav" and title_cond in ("Novostavba", "Vo výstavbe"):
            self.property_condition = title_cond

        # 8. Enforce realistic market price bounds (filters out 1 EUR placeholders and typos)
        if self.price_eur is not None:
            if self.transaction_type == "prenajom":
                if not (150 <= self.price_eur <= 10_000):
                    self.price_eur = None
                    self.price_per_sqm_eur = None
            else:
                if not (15_000 <= self.price_eur <= 5_000_000):
                    self.price_eur = None
                    self.price_per_sqm_eur = None

        # 9. Always recompute unit price (EUR/m2) from validated price and area
        if self.price_eur is not None and self.area_sqm is not None and self.area_sqm > 0:
            pps = round(self.price_eur / self.area_sqm, 2)
            if self.transaction_type == "prenajom":
                if 3.0 <= pps <= 60.0:
                    self.price_per_sqm_eur = pps
                else:
                    self.price_eur = None
                    self.price_per_sqm_eur = None
            else:
                if 300.0 <= pps <= 25_000.0:
                    self.price_per_sqm_eur = pps
                else:
                    self.price_eur = None
                    self.price_per_sqm_eur = None
        else:
            self.price_per_sqm_eur = None

        # 10. Clear invalid total_floors (single-floor placeholders or floor > total_floors)
        if self.total_floors is not None and (
            self.total_floors <= 1
            or (self.floor is not None and self.floor > self.total_floors)
            or (self.has_elevator and self.total_floors == 1)
        ):
            self.total_floors = None

        return self
