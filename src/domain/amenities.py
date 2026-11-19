"""Structured portal attribute helpers, three-valued balcony logic, and elevator resolution."""

from __future__ import annotations

import re
from typing import Any

from src.domain.reference import normalize_text, parse_positive_float, remove_diacritics
from src.domain.sections import DescriptionSections

# Area-exclusion phrases where "bez balkóna/loggie" means floor area excluding balcony, NOT absence of balcony
AREA_EXCLUSION_BALCONY_RE = re.compile(
    r"(?:"
    r"(?:vymer\w*|ploch\w*|rozloh\w*|interier\w*|\d+(?:[.,]\d+)?\s*(?:m2|m²))\s*(?:bytu\s*)?(?:\(\s*)?bez\s+(?:plochy\s+)?(?:balkon|loggi|lodzi|logi|teras)\w*(?:\s+a\s+pivnic\w*)?"
    r"|\(\s*bez\s+(?:plochy\s+)?(?:balkon|loggi|lodzi|logi|teras)\w*\s+a\s+pivnic\w*\s*\)"
    r")"
)

# Negated balcony/loggia/terrace expressions in Slovak ("bez balkóna", "nemá balkón", "balkón: nie")
NEGATED_BALCONY_RE = re.compile(
    r"\b(?:"
    r"bez\s+(?:\w+\s+){0,2}(?:balkon|loggi|lodzi|logi|teras)\w*"
    r"|(?:momentalne\s+|aktualne\s+)?(?:nema|nemaju|nedisponuje|neobsahuje|chyba)\s+(?:\w+\s+){0,3}(?:balkon|loggi|lodzi|logi|teras)\w*"
    r"|(?:balkon|loggi\w*|lodzi\w*|logi\w*|teras\w*)\s*[:=-]\s*(?:nie|0\b|ziadn\w*|nema)"
    r"|(?:balkon|loggi\w*|lodzi\w*|logi\w*|teras\w*)\s+je\s+(?:nie|0\b|ziadn\w*)\b"
    r"|nie\s+je\s+(?:k\s+dispozicii\s+)?(?:balkon|loggi|lodzi|logi|teras)\w*"
    r")\b"
)

# Shared staircase/landing/rooftop balcony or terrace expressions ("spoločný balkón", "komunitná terasa")
SHARED_BALCONY_RE = re.compile(
    r"\b(?:"
    r"(?:spolocn\w+|komunitn\w+|zdielan\w+)\s+(?:\w+\s+){0,2}(?:balkon|loggi|lodzi|logi|teras)\w*(?:\s+na\s+(?:medziposchod\w*|chodbe|poschodi|streche|najvyssom\s+poschodi))?"
    r"|(?:balkon|loggi\w*|lodzi\w*|logi\w*)\s+(?:je\s+|sa\s+nachadza\s+|nachadzajuc\w*\s+sa\s+)?na\s+(?:medziposchod\w*|chodbe)"
    r"|na\s+(?:medziposchod\w*|chodbe)\s+(?:je\s+|sa\s+nachadza\s+|k\s+dispozicii\s+)?(?:spolocn\w+\s+)?(?:balkon|loggi|lodzi|logi)\w*"
    r")\b"
)

# Hypothetical, planned, or neighbor balcony mentions ("možnosť spraviť loggiu", "bude prislúchať loggia")
HYPOTHETICAL_BALCONY_RE = re.compile(
    r"\b(?:"
    r"moznost\w*\s+(?:dorobit|spravit|vybudovat|pristavat|zriadit)\s+(?:si\s+)?(?:balkon|loggi|lodzi|logi|teras)\w*"
    r"|susedia\s+[^.;\n]{0,45}?(?:balkon|loggi|lodzi|logi|teras)\w*"
    r"|bude\s+(?:prisluchat|vybudovan\w*|dostavan\w*)\s+(?:aj\s+)?(?:balkon|loggi|lodzi|logi|teras)\w*"
    r"|planovan\w+\s+(?:vystavb\w*|dostavb\w*)\s+(?:balkon|loggi|lodzi|logi|teras)\w*"
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

# Negated elevator expressions in Slovak ("bez výťahu", "nemá výťah", "výťah: nie", "bez nutnosti výťahu")
NEGATED_ELEVATOR_RE = re.compile(
    r"\b(?:"
    r"bez\s+(?:nutnosti\s+)?(?:\w+\s+){0,2}vytah\w*"
    r"|(?:nema|nemaju|nedisponuje|neobsahuje|chyba)\s+(?:\w+\s+){0,2}vytah\w*"
    r"|vytah\w*\s*(?:[:=-]|\s+v\s+dome)?\s*(?:je\s+|sa\s+)?(?:nie|0\b|ziadn\w*|nema|nenachadza)"
    r"|nie\s+je\s+(?:k\s+dispozicii\s+)?vytah\w*"
    r")\b"
)


def build_attrs_map(attrs_list: list[dict[str, Any]] | None) -> dict[str, str]:
    """Flatten `parameters.attributes` (`[{"label": ..., "value": ...}]`) into a lookup dict."""
    result: dict[str, str] = {}
    for item in attrs_list or []:
        label = item.get("label")
        value = item.get("value")
        if not label or value is None or value in ("", "$undefined"):
            continue
        result[str(label).strip()] = str(value).strip()
    return result


def compute_exterior_area_from_attrs(attrs: dict[str, str]) -> float:
    """Sum structured balcony, loggia, terrace, and cellar areas (m2) from `attrs`."""
    total_ext = 0.0
    for label, val in attrs.items():
        label_clean = remove_diacritics(label).casefold()
        if "plocha" in label_clean and any(
            w in label_clean for w in ("balkon", "loggi", "lodzi", "teras", "pivnic")
        ):
            parsed = parse_positive_float(val)
            if parsed is not None and 0.5 <= parsed <= 250.0:
                total_ext += parsed
    return round(total_ext, 2)


def resolve_balcony_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool | None:
    """Resolve three-valued balcony status (`True`, `False`, or `None`) from structured attributes."""
    vyb_clean = remove_diacritics(vybavenie).casefold()
    if any(w in vyb_clean for w in ("balkon", "loggi", "lodzi", "teras")):
        return True

    saw_explicit_false = False
    for label, val in attrs.items():
        label_clean = remove_diacritics(label).casefold()
        if any(w in label_clean for w in ("balkon", "loggi", "lodzi", "teras")):
            val_clean = remove_diacritics(val).strip().casefold()
            if not val_clean or val_clean in ("-", "none"):
                continue
            if val_clean.startswith("0") or val_clean in ("nie", "false"):
                saw_explicit_false = True
            else:
                return True

    return False if saw_explicit_false else None


def has_elevator_from_attrs(attrs: dict[str, str], vybavenie: str = "") -> bool:
    """Check structured attributes (`parameters.attributes`) for an elevator ('výťah')."""
    vyb_clean = remove_diacritics(vybavenie).casefold()
    if "vytah" in vyb_clean:
        return True

    for label, val in attrs.items():
        label_clean = remove_diacritics(label).casefold()
        if "vytah" in label_clean:
            val_clean = remove_diacritics(val).strip().casefold()
            if (
                val_clean
                and not val_clean.startswith("0")
                and val_clean not in ("nie", "false", "-", "none")
            ):
                return True

    return False


def has_negated_balcony(text: str | None) -> bool:
    """Return True if the text explicitly states the absence of a private balcony/loggia/terrace."""
    if not text:
        return False
    cleaned = normalize_text(text)
    cleaned = AREA_EXCLUSION_BALCONY_RE.sub(" ", cleaned)
    if NEGATED_BALCONY_RE.search(cleaned):
        return True
    return bool(SHARED_BALCONY_RE.search(cleaned) and not has_balcony_from_text(text))


def has_shared_only_balcony(title: str | None, description: str | None) -> bool:
    """Return True when the listing mentions a shared landing/rooftop balcony/terrace and no private one."""
    combined = f"{title or ''} {description or ''}".strip()
    if not combined:
        return False
    cleaned = normalize_text(combined)
    if not SHARED_BALCONY_RE.search(cleaned):
        return False
    return not has_balcony_from_text(title) and not has_balcony_from_text(description)


def has_balcony_from_text(text: str | None) -> bool:
    """Detect positive private balcony, loggia, or terrace mentions after masking negations/homonyms."""
    if not text:
        return False

    cleaned = normalize_text(text)
    if cleaned.startswith(("http://", "https://", "/detail/")):
        cleaned = re.sub(r"[-/_]+", " ", cleaned)
    cleaned = AREA_EXCLUSION_BALCONY_RE.sub(" ", cleaned)
    cleaned = NEGATED_BALCONY_RE.sub(" ", cleaned)
    cleaned = SHARED_BALCONY_RE.sub(" ", cleaned)
    cleaned = HYPOTHETICAL_BALCONY_RE.sub(" ", cleaned)
    cleaned = KOSICE_TERASA_RE.sub(" ", cleaned)

    return bool(
        re.search(
            r"\b(?:dvoj)?(?:balkon\w*|loggi\w*|lodzi\w*|logi(?:a|e|i|o|u|ou|ami|ach)\b|teras\w*)\b",
            cleaned,
        )
    )


def resolve_balcony(
    attr_balcony: bool | None,
    title: str | None,
    url: str | None,
    sections: DescriptionSections,
) -> bool | None:
    """Resolve three-valued balcony flag (`True` / `False` / `None`) without double-negation bugs."""
    desc_apt = sections.non_location_text
    if has_shared_only_balcony(title, sections.full_text):
        return False
    if attr_balcony is True:
        return True
    if not has_negated_balcony(title) and has_balcony_from_text(title):
        return True
    if has_balcony_from_text(desc_apt):
        return True
    if (
        not has_negated_balcony(title)
        and not has_negated_balcony(desc_apt)
        and has_balcony_from_text(url)
    ):
        return True
    if attr_balcony is False or has_negated_balcony(title) or has_negated_balcony(desc_apt):
        return False
    return None


def has_negated_elevator(text: str | None) -> bool:
    """Return True if the text explicitly states the absence of an elevator ('bez výťahu')."""
    if not text:
        return False
    return bool(NEGATED_ELEVATOR_RE.search(normalize_text(text)))


def has_elevator_from_text(text: str | None) -> bool:
    """Detect non-negated elevator ('výťah') mentions in free text."""
    if not text:
        return False
    cleaned = normalize_text(text)
    cleaned = NEGATED_ELEVATOR_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\bvytah\w*\s+z\s+(?:katastr\w*|listu\s+vlastnictva)\b", " ", cleaned)
    return bool(re.search(r"\bvytah\w*\b", cleaned))


def resolve_elevator(
    raw_has_elevator: bool,
    title: str | None,
    sections: DescriptionSections,
    floor: int | None,
    total_floors: int | None,
    property_condition: str | None,
    is_modern_complex: bool,
) -> bool:
    """Resolve building elevator availability with unified thresholds and explicit negation respect."""
    desc_scope = sections.non_location_text
    negated = has_negated_elevator(title) or has_negated_elevator(desc_scope)

    if raw_has_elevator:
        # Respect explicit walk-up negation only on low-rise misclicks (total_floors <= 3)
        return not (
            negated
            and total_floors is not None
            and total_floors <= 3
            and not has_elevator_from_text(desc_scope)
        )

    if negated:
        return False

    is_multi_story = total_floors is None or total_floors >= 4
    return bool(
        has_elevator_from_text(title)
        or has_elevator_from_text(desc_scope)
        or (is_modern_complex and is_multi_story and (floor is None or floor >= 1))
        or (floor is not None and floor >= 5)
        or (total_floors is not None and total_floors >= 6)
        or (
            property_condition in ("Novostavba", "Vo výstavbe")
            and (
                (floor is not None and floor >= 4)
                or (total_floors is not None and total_floors >= 5)
            )
        )
    )
