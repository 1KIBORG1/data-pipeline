"""Reference datasets, geographic bounds, and low-level text/number parsers for Slovak real estate."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Valid building floor range in Slovakia (-2 = underground levels, 46 = Eurovea Tower)
MIN_FLOOR = -2
MAX_FLOOR = 46

# Geographic bounding box of the Slovak Republic (WGS84) with small border tolerance
SLOVAKIA_LAT_MIN, SLOVAKIA_LAT_MAX = 47.70, 49.65
SLOVAKIA_LON_MIN, SLOVAKIA_LON_MAX = 16.80, 22.60

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
APARTMENT_KEYWORDS: tuple[str, ...] = (
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

# Foreign border streets mistakenly placed under Slovak districts on the portal
FOREIGN_STREETS: frozenset[str] = frozenset({"am strandbad", "thulusdhoo"})

# Diacritic-stripped title stems for reserved listings (filtered only when `exclude_reserved=True`)
RESERVED_TITLE_STEMS: tuple[str, ...] = (
    "rezervovan",
    "rezervovn",
    "rezervvov",
    "rezervovali",
    "rezervaci",
    "rezerve",
    "reserve",
)

# Diacritic-stripped title stems for inactive, wanted, commercial, house, or foreign listings
# Note: GPS coordinates handle most foreign ads; country/border stems remain as fallback when GPS is missing.
REJECT_TITLE_STEMS: tuple[str, ...] = (
    # Inactive / sold / rented
    "predan",
    "prenajat",
    # Buyer wanted / demand / agency service / swap
    "kupim",
    "hladam",
    "ponuknite",
    "dopyt",
    "rychly predaj nehnutelnost",
    "asistencne realitne sluzby",
    "od katastra az po vypratanie",
    "vymen byt",
    "vymena bytu",
    "vykup nehnutelnost",
    "platba v hotovosti",
    "1-2 izb byt,hotovost",
    # Shared rooms / dormitories / auctions / partial ownership / transfers
    "samostatna izba",
    "samostatne izby",
    "spolubyvani",
    "slobodark",
    "odstupeni",
    "drazb",
    "polovicny podiel",
    "spoluvlastnicky podiel",
    # Whole buildings / commercial hospitality / non-residential spaces / garages / raw attics
    "penzion",
    "ubytovaci komplex",
    "developerskeho arealu",
    "apartmanovy dom",
    "apartmanoveho domu",
    "investicny areal",
    "prerobena budova",
    "investicna budova",
    "samostatna budova",
    "a polyfunkcny dom",
    "apartmanov + 2 domy",
    "kancelarsky priestor",
    "multifunkcny priestor",
    "obrovsky priestor",
    "zaujimavy priestor s pozemkom",
    "podkrovny priestor",
    "predaj garaze",
    "koja a parkovacie",
    "vodarenska studna",
    # Family houses / townhouses / cottages
    "mobilny dom",
    "bungalov",
    "villovy dom",
    "vidiecky dom",
    "mestsky dom",
    "v trojdome",
    "v dvojdome",
    "trojdom",
    "dvojdom",
    "byt v rd",
    "4i chyzka so zahradkou",
    "horska chata",
    # Portal QA test placeholders
    "testovaci inzerat",
    "test inzerat",
    "123 test",
    "skuska nova",
    "autofill addon",
    # Explicit rental phrases misclassified under sale
    "na dlhodoby prenajom",
    "prenajom - dlhodobo",
    "dam do prenajmu",
    "ponukame na prenajom",
    # Foreign property fallback stems (when GPS coordinates are omitted or spoofed)
    "chorvatsk",
    "ostrova ciovo",
    "ostrove ciovo",
    "v rakusk",
    "wolfsthal",
    "kittsee",
    "hainburg",
    "v rajke",
    "gruzinsk",
    "tbilisi",
    "dominikansk",
    "pri mori",
    "pri ciernom mori",
    "sunset resort pomorie",
    "more na dosah",
    "hurgad",
    "hurghad",
    "na bali",
    "maldiv",
    "thulusdhoo",
    "aguas nuevas",
    "v spanielsku",
    "v bulharsku",
    "v taliansku",
    "v dubaji",
)

# High-rise modern residential developments in Slovakia always equipped with elevators
MODERN_COMPLEX_STEMS: tuple[str, ...] = (
    "slnecnic",
    "matadork",
    "einpark",
    "vienna gate",
    "city park",
    "liget",
    "fuxova",
    "petrzalka city",
    "bory byvani",
    "sky park",
    "eurovea",
    "klingerka",
    "zwirn",
    "nuppu",
    "ceresne",
    "ovocne sady",
    "pri myte",
    "stein2",
    "guthaus",
    "zuckermandel",
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
    ("studiov", 1),
    ("1kk", 1),
)


def remove_diacritics(value: str) -> str:
    """Strip accents/diacritics from Unicode text using NFKD decomposition."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def normalize_dashes(text: str) -> str:
    """Normalize Unicode hyphens and dashes (`–`, `—`, `‐`, `‑`, `−`) to ASCII `-`."""
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text)


def normalize_text(value: str | None) -> str:
    """Lowercase, strip diacritics, and normalize Unicode dashes."""
    if not value:
        return ""
    return normalize_dashes(remove_diacritics(value).casefold())


def is_within_slovakia_gps(latitude: float | None, longitude: float | None) -> bool:
    """Return True if coordinates are omitted or fall within Slovakia's WGS84 bounding box."""
    if latitude is None or longitude is None:
        return True
    # Ignore unpopulated (0, 0) null-island placeholders
    if abs(latitude) < 0.01 and abs(longitude) < 0.01:
        return True
    return (
        SLOVAKIA_LAT_MIN <= latitude <= SLOVAKIA_LAT_MAX
        and SLOVAKIA_LON_MIN <= longitude <= SLOVAKIA_LON_MAX
    )


def parse_positive_float(value: Any) -> float | None:
    """Parse a positive number from int, float, or formatted Slovak string (`'145 000 €'`, `'71,44 m²'`)."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return round(number, 2) if number > 0 else None

    if isinstance(value, str):
        value = value.strip()
        if not value or value == "$undefined":
            return None

        # Strip m2 / m² unit tokens before extracting numeric digits
        value = re.sub(r"m[²2]", "", value, flags=re.IGNORECASE)
        value = re.sub(r"[\s\u00a0\u202f]+", "", value)
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
        if value.strip() == "$undefined":
            return None
        text = remove_diacritics(value).lower()
        if "prizemie" in text:
            return 0
        if "suteren" in text:
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
        if value.strip() == "$undefined":
            return None
        match = re.search(r"\d+", value)
        return int(match.group()) if match is not None else None

    return None


def clean_district(value: Any) -> str | None:
    """Strip the redundant `'okres '` prefix and normalize hyphen spacing in district names."""
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value or value == "$undefined":
        return None

    value = re.sub(r"^okres\s+", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s*-\s*", "-", value)
