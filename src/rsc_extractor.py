"""Next.js RSC Flight payload extractor for Nehnutelnosti.sk listing pages.

Unpacks the `advertisement` object from `self.__next_f.push([1, "..."])` chunks,
resolves out-of-line text references (`$ref` / JSON-LD fallback), and reports explicit
extraction statuses (`ok`, `inactive`, `bot_challenge`, `missing_payload`, `parse_error`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from parsel import Selector

_NEXT_F_PUSH_RE = re.compile(
    r"^\s*self\.__next_f\.push\((.*)\)\s*;?\s*$",
    flags=re.DOTALL,
)

# Large UI-only fields excluded from Bronze JSONB storage (cuts row size by ~85%)
BLOAT_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "similarAdvertisements",
    "otherFromAdvertiser",
    "devProjects",
    "media",
    "pois",
    "demographics",
    "oneSignalTags",
    "nearestTransport",
    "nearbyPois",
})

_BOT_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "__cf_chl_",
    "Attention Required! | Cloudflare",
    "<title>Just a moment...</title>",
)


def _unwrap_next_f_push(script_text: str) -> list[Any] | None:
    """Decode the JSON array argument inside `self.__next_f.push([...]);`."""
    match = _NEXT_F_PUSH_RE.match(script_text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _find_advertisement_obj(node: Any) -> dict[str, Any] | None:
    """Recursively locate the `advertisement` dictionary containing an `id` field."""
    if isinstance(node, dict):
        ad = node.get("advertisement")
        if isinstance(ad, dict) and ad.get("id"):
            return ad
        for value in node.values():
            found = _find_advertisement_obj(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_advertisement_obj(item)
            if found is not None:
                return found
    return None


def _slice_utf8_bytes(text: str, byte_len: int) -> str:
    """Slice `text` to at most `byte_len` UTF-8 bytes without splitting multi-byte chars."""
    raw = text.encode("utf-8")
    if len(raw) <= byte_len:
        return text
    return raw[:byte_len].decode("utf-8", errors="ignore")


def _extract_jsonld_description(sel: Selector) -> str | None:
    """Fallback: read `description` from `<script type="application/ld+json">` if present."""
    for raw_ld in sel.xpath('//script[@type="application/ld+json"]/text()').getall():
        try:
            obj = json.loads(raw_ld)
        except json.JSONDecodeError:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for item in candidates:
            if isinstance(item, dict):
                desc = item.get("description")
                if isinstance(desc, str) and desc.strip():
                    return desc.strip()
    return None


def _resolve_rsc_text_ref(ref_id: str, all_scripts: list[str], sel: Selector) -> str | None:
    """Resolve an out-of-line Next.js RSC text pointer (e.g. `"$2f"` -> `"2f:T<hex>,..."`)."""
    # Decode all Flight string payloads once
    chunks: list[str] = []
    for sc in all_scripts:
        arr = _unwrap_next_f_push(sc)
        if arr is not None and len(arr) >= 2 and isinstance(arr[1], str):
            chunks.append(arr[1])

    pattern = re.compile(
        rf"(?:^|\n){re.escape(ref_id)}:T([0-9a-fA-F]+),(.*)",
        flags=re.DOTALL,
    )

    for idx, chunk_val in enumerate(chunks):
        match = pattern.search(chunk_val)
        if match is None:
            continue

        byte_len = int(match.group(1), 16)
        inline_rest = match.group(2)
        if inline_rest:
            return _slice_utf8_bytes(inline_rest, byte_len)

        # Next.js often emits the header `<id>:T<len>,` in one push() and the body in the next
        if idx + 1 < len(chunks):
            next_val = chunks[idx + 1]
            if next_val and not re.match(r"^[0-9a-fA-F]+:(?:I\[|\{|\[)", next_val):
                return _slice_utf8_bytes(next_val, byte_len)

    return _extract_jsonld_description(sel)


def find_ad_script(html: str) -> str | None:
    """Locate the `<script>` tag containing the serialized advertisement Flight payload."""
    sel = Selector(text=html)
    ad_script = sel.xpath(
        """//script[contains(text(), 'self.__next_f.push(') and contains(text(), '\\"advertisement\\"')]/text()"""
    ).get()
    return ad_script.strip() if ad_script else None


def strip_bloat_fields(ad: dict[str, Any]) -> dict[str, Any]:
    """Strip bulky UI arrays and seller PII before persisting Bronze JSONB."""
    cleaned = {k: v for k, v in ad.items() if k not in BLOAT_TOP_LEVEL_KEYS}
    advertiser = cleaned.get("advertiser")
    if isinstance(advertiser, dict):
        # Retain only non-PII seller classification metadata (GDPR hygiene)
        cleaned["advertiser"] = {
            k: advertiser[k] for k in ("type", "isPro") if k in advertiser
        }
    return cleaned


def extract_ad_with_status(html: str) -> tuple[dict[str, Any] | None, str]:
    """Extract the Next.js RSC `advertisement` dict along with a diagnostic status code.

    Status codes:
        - `"ok"`: Active advertisement payload unpacked successfully.
        - `"inactive"`: Listing shows the expired banner or `ad["isActive"] is False`.
        - `"bot_challenge"`: Page resembles a Cloudflare/anti-bot interstitial challenge.
        - `"missing_payload"`: No advertisement RSC Flight chunk found in HTML.
        - `"parse_error"`: RSC script matched, but JSON/Flight structure failed to decode.
    """
    if not html:
        return None, "missing_payload"

    if any(marker in html for marker in _BOT_CHALLENGE_MARKERS):
        return None, "bot_challenge"

    if "Tento inzerát už nie je aktuálny" in html:
        return None, "inactive"

    sel = Selector(text=html)
    candidate_scripts = sel.xpath(
        """//script[contains(text(), 'self.__next_f.push(') and contains(text(), '\\"advertisement\\"')]/text()"""
    ).getall()
    if not candidate_scripts:
        return None, "missing_payload"

    ad: dict[str, Any] | None = None
    for ad_script in candidate_scripts:
        payload = _unwrap_next_f_push(ad_script)
        if payload is None or len(payload) < 2 or not isinstance(payload[1], str):
            continue

        for line in payload[1].splitlines():
            if '"advertisement"' not in line or ":" not in line:
                continue
            try:
                data = json.loads(line[line.index(":") + 1 :])
            except json.JSONDecodeError:
                continue
            ad = _find_advertisement_obj(data)
            if ad is not None:
                break
        if ad is not None:
            break

    if ad is None:
        return None, "parse_error"

    if ad.get("isActive") is False:
        return ad, "inactive"

    # Resolve out-of-line RSC Flight text chunk pointer for long descriptions (e.g. "$2f")
    desc = ad.get("description")
    if isinstance(desc, str) and desc.startswith("$") and len(desc) > 1 and desc != "$undefined":
        ref_id = desc[1:]
        all_scripts = sel.xpath(
            "//script[contains(text(), 'self.__next_f.push(')]/text()"
        ).getall()
        resolved = _resolve_rsc_text_ref(ref_id, all_scripts, sel)
        ad["description"] = resolved if resolved is not None else ""
    elif desc == "$undefined":
        ad["description"] = ""

    return ad, "ok"


def extract_ad_dict(html: str) -> dict[str, Any] | None:
    """Unpack the Next.js RSC Flight payload and return the raw advertisement dict (or None)."""
    ad, status = extract_ad_with_status(html)
    return ad if status == "ok" else None
