"""Section-aware parser for Slovak real estate listing descriptions.

Slovak real estate descriptions typically follow recurring structural headings:
`ZÁKLADNÉ INFORMÁCIE / BYT`, `DISPOZÍCIA`, `BYTOVÝ DOM`, `LOKALITA / OKOLIE`,
`CENA / FINANCOVANIE`, and `KONTAKT`.

Segmenting the description prevents location prose ("v blízkosti OC Eurovea",
"nákupné centrum Slnečnice Market", "kúpeľský park", "OC Terasa") and agency
disclaimers from contaminating apartment-level attribute extractors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.domain.reference import normalize_text

_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "location",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:lokalita|okolie|obcianska\s+vybavenost|vyhody\s+lokality|o\s+lokalite|dostupnost|poloha)\b[:\-–\s]*.*$"
        ),
    ),
    (
        "disposition",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:dispozicia(?:\s+bytu)?|dispozicne\s+riesenie|rozlozenie\s+izieb)\b[:\-–\s]*.*$"
        ),
    ),
    (
        "building",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:bytovy\s+dom|o\s+bytovom\s+dome|stav\s+bytoveho\s+domu|budova|o\s+projekte|projekt)\b[:\-–\s]*.*$"
        ),
    ),
    (
        "price",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:cena|financovanie|platobny\s+plan|mesacne\s+naklady|naklady\s+na\s+byvanie)\b[:\-–\s]*.*$"
        ),
    ),
    (
        "contact",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:kontakt|obhliadk\w*|o\s+developerovi|o\s+realitnej\s+kancelarii|realitny\s+servis)\b[:\-–\s]*.*$"
        ),
    ),
    (
        "apartment",
        re.compile(
            r"^\s*(?:[-•*#]+\s*)?(?:zakladne\s+(?:informacie|udaje|info)|parametre|technicke\s+udaje|charakteristika|byt|interier|popis(?:\s+nehnutelnosti|\s+bytu)?|vyhody|hlavne\s+benefity|stav\s+nehnutelnosti|vymera)\b[:\-–\s]*.*$"
        ),
    ),
)


@dataclass(frozen=True)
class DescriptionSections:
    """Segmented views of a Slovak apartment description."""

    full_text: str
    apartment_text: str     # Excludes LOKALITA and KONTAKT prose
    non_location_text: str  # Excludes LOKALITA and KONTAKT (includes BYT, DISPOZÍCIA, DOM, CENA)
    building_text: str
    location_text: str


_BULLET_KV_RE = re.compile(r"^\s*[-•*▪▸✓✔]+\s*[^:\n]{1,40}:\s*\S+")


def _is_section_heading(stripped: str, norm_line: str) -> str | None:
    """Return section name if `stripped` is a true multi-line section header, else None.

    Distinguishes real section headings (`LOKALITA:`, `Lokalita a okolie`, `LOKALITA – VRAKUŇA`,
    `LOKALITA: BRATISLAVA`) from inline key-value parameter bullets (`* Lokalita: Rajčianska ul.`),
    which must NOT hijack subsequent apartment parameter bullets like `* Poschodie: 5/8`.
    """
    if len(norm_line) > 65 or norm_line.endswith("."):
        return None

    # Bulleted key-value lines (`* Lokalita: ...`, `- lokalita: ...`, `• Lokalita: ...`)
    # are single-line parameter rows, never multi-line section headers.
    if _BULLET_KV_RE.match(stripped):
        return None

    # Non-bullet mixed-case `Key: Value` lines (e.g. `Lokalita: Vrakuňa`) are only treated
    # as section headers if the value is empty (`Lokalita:`) or the line is ALL CAPS
    # (`LOKALITA: BRATISLAVA – VRAKUŇA`).
    if ":" in stripped:
        _, after_colon = stripped.split(":", 1)
        if after_colon.strip() and not stripped.isupper():
            return None

    for sec_name, rx in _SECTION_PATTERNS:
        if rx.match(norm_line):
            return sec_name
    return None


def split_description_sections(description: str | None) -> DescriptionSections:
    """Split a listing description into semantic sections by Slovak headings.

    Only genuine heading lines trigger a section switch, so inline key-value bullets
    (`* Lokalita: ...`) and prose sentences (`Lokalita ponúka...`) never discard
    subsequent apartment facts.
    """
    if not description:
        return DescriptionSections("", "", "", "", "")

    buckets: dict[str, list[str]] = {
        "apartment": [],
        "disposition": [],
        "building": [],
        "location": [],
        "price": [],
        "contact": [],
    }
    current = "apartment"

    for raw_line in description.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        norm_line = normalize_text(stripped)
        matched_section = _is_section_heading(stripped, norm_line)
        if matched_section is not None:
            current = matched_section

        buckets[current].append(stripped)

    full_text = description.strip()
    apartment_parts = buckets["apartment"] + buckets["disposition"] + buckets["building"] + buckets["price"]
    non_location_text = "\n".join(apartment_parts) if apartment_parts else full_text
    apt_core_parts = buckets["apartment"] + buckets["disposition"] + buckets["price"]
    apartment_text = "\n".join(apt_core_parts) if apt_core_parts else non_location_text

    return DescriptionSections(
        full_text=full_text,
        apartment_text=apartment_text,
        non_location_text=non_location_text,
        building_text="\n".join(buckets["building"]),
        location_text="\n".join(buckets["location"]),
    )
