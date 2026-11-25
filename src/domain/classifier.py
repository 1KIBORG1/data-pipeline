"""Entity and geographic scope filtering for Slovak apartment sale listings."""

from __future__ import annotations

import re

from src.domain.reference import (
    APARTMENT_KEYWORDS,
    FOREIGN_STREETS,
    REJECT_TITLE_STEMS,
    RESERVED_TITLE_STEMS,
    SLOVAK_DISTRICTS,
    is_within_bratislava_gps,
    is_within_slovakia_gps,
    normalize_text,
)

# Neighboring districts, regions, and commuter-belt municipalities outside Bratislava I-V
_BRATISLAVA_EXTERNAL_LOC_RE = re.compile(
    r"\b(?:"
    r"okres\s+(?:senec|dunajska\s+streda|trnava|galanta|pezinok|malacky|hlohovec)"
    r"|v\s+(?:tt|trnavskom|nitrianskom)\s+kraji"
    r"|v\s+(?:hamuliakove|rovinke|dunajskej\s+luznej|miloslavove|bernolakove"
    r"|chorvatskom\s+grobe|ivanke\s+pri\s+dunaji|stupave|marianke|dvornikoch)"
    r")\b"
)


def validate_entity_and_scope(
    *,
    is_active: bool,
    title: str | None,
    description: str | None,
    city: str | None,
    district: str | None,
    street: str | None,
    latitude: float | None,
    longitude: float | None,
    price_eur: int | None,
    price_display: str | None,
    area_sqm: float | None,
    floor: int | None,
    total_floors: int | None,
    rooms_count: int | None,
    vlastnictvo: str | None,
    construction: str | None,
    plocha_pozemku: float | None,
    zastavana_plocha: float | None,
    exclude_reserved: bool,
) -> None:
    """Raise `ValueError` if a listing is not an active Slovak apartment sale."""
    if not is_active:
        raise ValueError("Listing is marked inactive on Nehnutelnosti.sk")

    # 1. Geographic scope (Slovakia WGS84 bounding box + border street check)
    if not is_within_slovakia_gps(latitude, longitude):
        raise ValueError(f"Listing GPS coordinates ({latitude}, {longitude}) are outside Slovakia")

    street_clean = normalize_text(street).strip()
    if street_clean in FOREIGN_STREETS:
        raise ValueError(f"Listing street indicates a foreign property: {street}")

    # 2. Monthly-rent unit in structured portal price string (e.g. '850 €/mes.')
    price_disp_clean = normalize_text(price_display)
    if "/mes" in price_disp_clean or "mesacne" in price_disp_clean:
        raise ValueError("Listing portal price indicates a monthly rental (/mes.)")

    title_clean = normalize_text(title)
    desc_clean = normalize_text(description)
    vlast_clean = normalize_text(vlastnictvo)
    constr_clean = normalize_text(construction)

    # 3. Title entity filters (QA placeholders, optional reserved filter, wanted/commercial/house)
    if title_clean:
        if title_clean in ("test", "skuska", "inzerat"):
            raise ValueError("Listing is an internal QA test placeholder")

        if exclude_reserved:
            reserved_word = next(
                (stem for stem in RESERVED_TITLE_STEMS if stem in title_clean),
                None,
            )
            if reserved_word is not None:
                raise ValueError(f"Apartment title contains a rejected stem: {reserved_word}")

        rejected_word = next(
            (stem for stem in REJECT_TITLE_STEMS if stem in title_clean),
            None,
        )
        if rejected_word is not None:
            raise ValueError(f"Apartment title contains a rejected stem: {rejected_word}")

        if re.search(r",\s*hotovost\b", title_clean):
            raise ValueError("Apartment title contains a rejected stem: hotovost")

        if title_clean.startswith(("na prenajom", "prenajom ", "parkovacie miest", "podkrovie ")):
            raise ValueError("Apartment title indicates a rental, parking, or raw attic listing")

        if price_eur is None:
            tc_no_inv = re.sub(
                r"\b(?:vhodn\w*|idealn\w*)\s+(?:aj\s+)?na\s+(?:dlhodob\w*\s+(?:i\s+|aj\s+)?|kratkodob\w*\s+)*(?:prenajom|kancelari\w*)\b",
                "",
                title_clean,
            )
            if (
                re.search(r"\b(?:na\s+prenajom|prenajom|prenajmem|prenajmeme|vymena)\b", tc_no_inv)
                and "predaj" not in tc_no_inv
                and "predam" not in tc_no_inv
            ):
                raise ValueError("Listing is a rental or swap without a sale price")

        # Commercial non-residential spaces lacking residential apartment designation
        if re.search(
            r"\b(?:nebytov\w*\s+priestor\w*|obchodn\w*\s+priestor\w*|kancelarsk\w*\s+priestor\w*|skladov\w*\s+priestor\w*)\b",
            title_clean,
        ):
            tc_no_hyp = re.sub(
                r"\bmoznost\w*\s+(?:premeny|prerobenia|zmeny)\s+na\s+[^,.;]+",
                "",
                title_clean,
            )
            if (
                not re.search(
                    r"\b(?:byt\w*|holobyt\w*|apartman\w*|garson\w*|garzon\w*|loft\w*|studi\w*)\b",
                    tc_no_hyp,
                )
                or "kancelaria" in tc_no_hyp
                or "kancelarsk" in tc_no_hyp
            ):
                raise ValueError("Listing title indicates a commercial non-residential space")

        # Divided multi-unit bundles ("2 byty v 1") or house bundles
        tc_no_moz = re.sub(
            r"\bmoznost\w*\s+(?:vytvorenia\s+)?\d+\s+bytov\w*\s+jednot\w*",
            "",
            title_clean,
        )
        if re.search(
            r"\b(?:bytov\w+\s+dom\s+s\s+\d+|predaj\s+bytoveho\s+domu|trojpodlazn\w+\s+bytov\w+\s+dom"
            r"|vila\s+s\s+\d+\s+byt\w*|(?:rodinny\s+)?dom\s+s\s+(?:\d+|dvomi|dvoma|tromi)\s+bytov\w*\s+jednotk\w*"
            r"|(?:dva|tri|styri)\s+(?:\d\s*-?\s*izb\w*\s*)?byty\s+v\s+(?:1\s+|jednom\s+)?(?:rodinnom|tehlovom)\s+dome"
            r"|(?:2|3|dva|tri)\s+byty\s+v\s+(?:1|jednom)\b"
            r"|(?:2|3|4|5|6|7|8)\s*x\s*byt\b"
            r"|(?:2|3|4|5|6|7|8|dve|dvomi|tri)\s+(?:kompletne\s+zariaden\w+\s+)?(?:samostatn\w+\s+)?byt(?:ov\w+|\.)\s+jednot\w*"
            r"|\d\s*i\.?\s*(?:rodinny\s+)?(?:dom|rd)\b)\b"
            r"|\+\s*\d+\s*dom",
            tc_no_moz,
        ):
            raise ValueError("Listing title indicates a multi-unit bundle or house")

        # Non-residential spaces, standalone storage/utility units, houses, and cottages when no apartment keyword is present
        tc_for_apt_kw = re.sub(
            r"\b(?:v\s+cene|za\s+cenu)\s+(?:mini\s+)?(?:\d+\s*-?\s*izb\w*\s+)?(?:bytu|garson\w*|garzon\w*)\b",
            " ",
            title_clean,
        )
        has_apt_kw = any(kw in tc_for_apt_kw for kw in APARTMENT_KEYWORDS)
        if not has_apt_kw:
            has_room_layout = bool(re.search(r"\b[1-6]\s*(?:-?\s*izb|\s*i\b)", tc_for_apt_kw))
            if not has_room_layout and re.search(
                r"\b(?:pivnicn\w*\s+(?:kobk\w*|koja)|skladov\w*\s+(?:kobk\w*|koja)|koja"
                r"|samostatn\w*\s+(?:garaz\w*|pivnic\w*)|vodarensk\w*\s+studn\w*|studn(?:a|u|e))\b",
                tc_for_apt_kw,
            ):
                raise ValueError("Listing title indicates a standalone storage, garage, or utility unit")

        if not has_apt_kw and "panelov" not in constr_clean:
            if re.search(r"\bpriestor\b", tc_for_apt_kw):
                is_qualified_space = bool(
                    re.search(
                        r"\b(?:podkrovn\w*|multifunkcn\w*|komercn\w*|prevadzkov\w*|investicn\w*"
                        r"|exkluzivn\w*|unikatn\w*|zaujimav\w*|obrovsk\w*|\d+\s*-?\s*izbov\w*)\s+priestor\b"
                        r"|\bpriestor\b[^,.;]*\bs\s+pozemkom\b",
                        tc_for_apt_kw,
                    )
                )
                has_residential_context = bool(
                    re.search(
                        r"\b(?:byvani\w*|domov\w*|[1-6]\s*-?\s*izb\w*|poschod\w*|balkon\w*|loggi\w*|lodzi\w*)\b",
                        tc_for_apt_kw,
                    )
                )
                if is_qualified_space or not has_residential_context:
                    raise ValueError("Listing title indicates a generic space without apartment designation")

            tc_no_loc = (
                tc_for_apt_kw.replace("zochova chata", "")
                .replace("dom-realit", "")
                .replace("dom realit", "")
            )
            tc_no_loc = re.sub(
                r"\b(?:"
                r"(?:ako\s+)?v(?:o)?\s+(?:\w+\s+){0,2}(?:dome|domoch|viladome|viladomoch)"
                r"|(?:pod|pred|za|pri|vedla|okolo)\s+dom(?:om|u|e)"
                r"|dom\s+(?:[a-df-rt-uwyz0-9]|\d+)\b"
                r")\b",
                " ",
                tc_no_loc,
            )
            if re.search(
                r"\b(?:dom(?:u|e|y|om|och|ami)?\b|rodinnych\s+domov\b|viladom(?:u|y|ov)?\b"
                r"|vila\b|vily\b|vilu\b|villa\b|chata\b|chaty\b|chatu\b|chate\b|chalup\w*"
                r"|chyzk\w*\s+so\s+(?:zahrad\w*|pozemk\w*)"
                r"|podnikatelsk\w+\s+objekt\w*|budov\w+\s+na\s+podnikanie|polyfunkcn\w+\s+objekt\w*"
                r"|administrativn\w+\s+(?:budov\w*|objekt\w*))\b",
                tc_no_loc,
            ):
                raise ValueError("Listing title indicates a house or commercial building rather than an apartment")

    # 4. Description & structural attribute checks (buyer-wanted DOPYT, LV priestor, townhouses, 2-in-1 splits)
    if desc_clean and re.search(
        r"\b(?:"
        r"pre\s+(?:konkretneho\s+|nasich\s+|solventneho\s+)?klient\w+\s+hladam\w*"
        r"|hladam\w*\s+na\s+kupu\s+\d"
        r"|hladam\w*\s+pre\s+(?:konkretneho\s+)?klient\w+"
        r"|na\s+predaj\s+nebytov\w+\s+priestor"
        r"|aktualne\s+je\s+nebytov\w+\s+priestor"
        r"|(?:na\s+lv|na\s+liste\s+vlastnictva)\s+(?:je\s+)?veden\w+\s+ako\s+(?:nebytov\w+\s+)?priestor"
        r"|sucastou\s+(?:trojdomu|dvojdomu)"
        r"|(?:rozlozen\w+\s+na|priestor\s+na)\s+(?:troch|3)\s+podlaziach"
        r"|rozdelen\w+\s+na\s+(?:dva|2|tri|3)\s+(?:samostatn\w+\s+)?byty"
        r"|rozdelen\w+\s+na\s+\d\s*-?\s*izbov\w+\s+byt\s+a\s+garson\w*"
        r")\b",
        desc_clean,
    ):
        raise ValueError(
            "Description indicates a buyer-wanted DOPYT, non-residential space, townhouse, or split multi-unit package"
        )

    # Low-rise townhouses/villas sold with private land plots (never triggered on panel buildings)
    if (
        "panelov" not in constr_clean
        and plocha_pozemku is not None
        and plocha_pozemku > 0
        and (area_sqm is None or abs(plocha_pozemku - area_sqm) >= 2.0)
        and total_floors in (1, 2, None)
        and floor in (0, 1, 2, None)
        and (
            (zastavana_plocha is not None and zastavana_plocha < 120.0)
            or "firemne" in vlast_clean
            or "trojdom" in desc_clean
            or "dvojdom" in desc_clean
            or "rodinneho domu" in desc_clean
        )
    ):
        raise ValueError("Structural attributes indicate a townhouse or house with private land plot")

    if (
        total_floors == 1
        and (rooms_count or 0) >= 5
        and re.search(r"(?<!bytovy\s)\bdom\s+sa\s+nachadza\b", desc_clean)
    ):
        raise ValueError("Listing is a multi-room row house with total_floors=1")

    # 5. Official Slovak district whitelist & Bratislava location-spoofing check (GPS + commuter belt)
    if not city and not district:
        raise ValueError("Listing has neither city nor district in Slovakia")
    if district is not None and district not in SLOVAK_DISTRICTS:
        raise ValueError(f"District is outside the 79 official districts of Slovakia: {district}")

    claims_bratislava = bool(
        (district and district.startswith("Bratislava"))
        or (city and city.startswith("Bratislava"))
    )
    if claims_bratislava:
        if not is_within_bratislava_gps(latitude, longitude):
            raise ValueError(
                f"Listing claims Bratislava location but GPS ({latitude}, {longitude}) falls outside Bratislava"
            )
        if _BRATISLAVA_EXTERNAL_LOC_RE.search(f"{title_clean} {desc_clean}"):
            raise ValueError("Listing spoofs Bratislava location for a property in another district")
