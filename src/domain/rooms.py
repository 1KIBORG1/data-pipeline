"""Room-count extraction and priority resolution for Slovak apartment listings."""

from __future__ import annotations

import re

from src.domain.reference import (
    MIN_AREA_BY_ROOMS,
    ROOMS_BY_SUBCATEGORY,
    WORD_ROOMS_MAP,
    normalize_text,
)


def _parse_room_token(text: str) -> int | None:
    """Parse a single room-count token from normalized text."""
    if re.search(r"(?<!\d[.,])(?<![\dA-Za-z])(?:2|dvoj)\s*-?\s*gar[sz]on", text):
        return 2

    match_rooms = re.search(
        r"(?<![.,\d])([1-9])(?:[.,]5\.?|\.)?(?:\s*(?:-\s*)?(?:izb|izieb)|\s*-\s*i\b|i\b|\s*\+\s*(?:kk|k|1)\b)",
        text,
    )
    if match_rooms:
        return int(match_rooms.group(1))

    if "apartman" in text and "studio" in text:
        return None

    for stem, count in WORD_ROOMS_MAP:
        if stem in text:
            return count

    return None


def extract_rooms_from_title(title: str | None) -> int | None:
    """Extract unambiguous single-unit room count from a listing title.

    Defers (`None`) on multi-unit developer ranges (`'1, 2 a 3 izbové byty'`, `'1- až 4-izbové'`)
    so the structured `category_sub` attribute is used instead.
    """
    if not title:
        return None

    tc = normalize_text(title)

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

    tc_half_safe = re.sub(r"\b([1-9])\s+a\s+1/2\b", r"\1.5", tc)
    tc_half_safe = re.sub(r"(?<=\d),(?=5(?:$|[^0-9]))", ".", tc_half_safe)

    m_range = re.search(
        r"\b([1-9])\s*-?\s*(?:(?:,\s*|\ba\b|\baz\b|\balebo\b|/|-)\s*([1-9])\s*-?\s*)+(?:izb|i\b)",
        tc_half_safe,
    )
    if m_range and m_range.group(1) != m_range.group(2):
        return None

    distinct_rooms = set(re.findall(r"(?<![.,\d])([1-9])(?:\s*-?\s*izb)", tc_half_safe))
    if len(distinct_rooms) >= 2 and re.search(r"\b(?:byty|apartmany)\b", tc_half_safe):
        return None

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

        m_combined = re.search(
            r"\bspojen\w+\s+([1-9])\s+a\s+([1-9])\s*-?\s*(?:izb\w*|i\b)",
            tc_half_safe,
        )
        if m_combined:
            return int(m_combined.group(1)) + int(m_combined.group(2))

    return _parse_room_token(tc_half_safe)


def extract_rooms_from_description(description: str | None) -> int | None:
    """Extract room count from description when missing from attributes and title."""
    if not description:
        return None
    cleaned = normalize_text(description)
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


def resolve_rooms_count(
    raw_rooms: int | None,
    category_sub: str | None,
    title: str | None,
    description: str | None,
    area_sqm: float | None,
) -> int | None:
    """Resolve room count in priority order: title -> category_sub -> raw attr -> description."""
    valid_raw = raw_rooms if (raw_rooms is not None and 1 <= raw_rooms <= 10) else None
    sub_rooms = ROOMS_BY_SUBCATEGORY.get(category_sub or "")
    title_rooms = extract_rooms_from_title(title)

    if title_rooms is not None:
        return title_rooms
    if sub_rooms is not None:
        if category_sub in ("FIVE_AND_MORE_ROOM_APARTMENT", "FIVE_PLUS_ROOM_APARTMENT"):
            return valid_raw if (valid_raw is not None and valid_raw >= 5) else 5
        return sub_rooms

    fallback_raw = valid_raw or extract_rooms_from_description(description)
    if (
        fallback_raw is not None
        and fallback_raw >= 4
        and area_sqm is not None
        and area_sqm < MIN_AREA_BY_ROOMS.get(fallback_raw, 42.0)
    ):
        return None
    return fallback_raw
