"""Pydantic v2 validation and normalization model for Slovak apartment listings."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from src.domain.amenities import (
    NEGATED_BALCONY_RE,
    has_balcony_from_text,
    has_elevator_from_text,
    has_negated_balcony,
    has_negated_elevator,
    has_shared_only_balcony,
    resolve_balcony,
    resolve_elevator,
)
from src.domain.area import (
    compute_exact_portal_area,
    extract_area_from_text,
    extract_price_from_text,
    fix_missing_decimal_area,
    resolve_pure_apartment_area,
)
from src.domain.classifier import validate_entity_and_scope
from src.domain.floor_condition import (
    extract_floor_and_total_from_text,
    has_apartment_complete_reconstruction,
    infer_property_condition,
    is_ground_floor_text,
    is_modern_complex_title,
    resolve_floor_and_total,
    resolve_property_condition,
)
from src.domain.reference import (
    APARTMENT_KEYWORDS,
    MAX_FLOOR,
    MIN_AREA_BY_ROOMS,
    MIN_FLOOR,
    MODERN_COMPLEX_STEMS,
    REJECT_TITLE_STEMS,
    RESERVED_TITLE_STEMS,
    ROOMS_BY_SUBCATEGORY,
    SLOVAK_DISTRICTS,
    clean_district,
    normalize_text,
    parse_floor,
    parse_positive_float,
    parse_rooms_count,
)
from src.domain.reference import (
    normalize_dashes as _normalize_dashes,
)
from src.domain.reference import (
    remove_diacritics as _remove_diacritics,
)
from src.domain.rooms import (
    extract_rooms_from_description,
    extract_rooms_from_title,
    resolve_rooms_count,
)
from src.domain.sections import split_description_sections

__all__ = [
    "APARTMENT_KEYWORDS",
    "ApartmentModel",
    "MAX_FLOOR",
    "MIN_AREA_BY_ROOMS",
    "MIN_FLOOR",
    "MODERN_COMPLEX_STEMS",
    "NEGATED_BALCONY_RE",
    "REJECT_TITLE_STEMS",
    "RESERVED_TITLE_STEMS",
    "ROOMS_BY_SUBCATEGORY",
    "SLOVAK_DISTRICTS",
    "_normalize_dashes",
    "_remove_diacritics",
    "clean_district",
    "extract_area_from_text",
    "extract_floor_and_total_from_text",
    "extract_price_from_text",
    "extract_rooms_from_description",
    "extract_rooms_from_title",
    "has_apartment_complete_reconstruction",
    "has_balcony_from_text",
    "has_elevator_from_text",
    "has_negated_balcony",
    "has_negated_elevator",
    "has_shared_only_balcony",
    "infer_property_condition",
    "is_ground_floor_text",
    "parse_floor",
    "parse_positive_float",
    "parse_rooms_count",
    "resolve_pure_apartment_area",
]


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

    # Raw context fields used during cross-field validation (excluded from Silver DB dump)
    description: str | None = Field(default=None, exclude=True)
    umiestnenie: str | None = Field(default=None, exclude=True)
    vlastnictvo: str | None = Field(default=None, exclude=True)
    street: str | None = Field(default=None, exclude=True)
    latitude: float | None = Field(default=None, exclude=True)
    longitude: float | None = Field(default=None, exclude=True)
    price_display: str | None = Field(default=None, exclude=True)
    price_note: str | None = Field(default=None, exclude=True)
    construction: str | None = Field(default=None, exclude=True)
    year_of_construction: int | None = Field(default=None, exclude=True)
    year_of_construction_lookup: str | None = Field(default=None, exclude=True)
    rok_kolaudacie: float | None = Field(default=None, exclude=True)
    plocha_pozemku: float | None = Field(default=None, exclude=True)
    zastavana_plocha: float | None = Field(default=None, exclude=True)
    exterior_area: float | None = Field(default=None, exclude=True)
    is_active: bool = Field(default=True, exclude=True)
    exclude_reserved: bool = Field(default=False, exclude=True)

    @field_validator("transaction_type", mode="before")
    @classmethod
    def normalize_transaction_type(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped == "$undefined":
            return None
        return _remove_diacritics(stripped).casefold() or None

    @field_validator("price_eur", mode="before")
    @classmethod
    def normalize_price_eur(cls, value: Any) -> int | None:
        parsed = parse_positive_float(value)
        return round(parsed) if parsed is not None else None

    @field_validator(
        "area_sqm",
        "price_per_sqm_eur",
        "plocha_pozemku",
        "zastavana_plocha",
        "exterior_area",
        "rok_kolaudacie",
        mode="before",
    )
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
            stripped = value.strip()
            if not stripped or stripped == "$undefined":
                return None
            return stripped.upper()
        return value

    @field_validator(
        "title",
        "property_condition",
        "city",
        "street",
        "construction",
        "price_display",
        "price_note",
        "year_of_construction_lookup",
        mode="before",
    )
    @classmethod
    def clean_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            collapsed = re.sub(r"\s+", " ", value).strip()
            if not collapsed or collapsed == "$undefined":
                return None
            return collapsed
        return value

    def _apply_price_bounds(self) -> None:
        """Null out unrealistic price placeholders (e.g. `1 EUR`, rental monthly rate under sale)."""
        if self.price_eur is None:
            return
        if self.transaction_type == "prenajom":
            if not (150 <= self.price_eur <= 10_000):
                self.price_eur = None
                self.price_per_sqm_eur = None
        else:
            if not (15_000 <= self.price_eur <= 5_000_000):
                self.price_eur = None
                self.price_per_sqm_eur = None

    @model_validator(mode="after")
    def validate_apartment_consistency(self) -> ApartmentModel:
        """Apply cross-field normalization and filtering in a single pass."""
        # Preserve raw portal unitPrice before bounds checks clear invalid price_per_sqm_eur
        raw_portal_unit_price = self.price_per_sqm_eur
        raw_total_floors_input = self.total_floors

        # 1. Pre-validate realistic market price bounds & recover fallback purchase price from title
        self._apply_price_bounds()
        if self.price_eur is None and self.transaction_type != "prenajom":
            self.price_eur = extract_price_from_text(self.title)

        # 2. Normalize Slovak city/district hierarchy
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

        # 3. Segment description once into apartment / building / location / contact sections
        sections = split_description_sections(self.description)

        # 4. Resolve floor & total_floors (unified Slovak 0-based indexing)
        self.floor, self.total_floors = resolve_floor_and_total(
            raw_floor=self.floor,
            raw_total_floors=self.total_floors,
            umiestnenie=self.umiestnenie,
            title=self.title,
            sections=sections,
        )

        # 5. Resolve room count (title -> category_sub -> attribute -> description)
        self.rooms_count = resolve_rooms_count(
            raw_rooms=self.rooms_count,
            category_sub=self.category_sub,
            title=self.title,
            description=sections.non_location_text,
            area_sqm=self.area_sqm,
        )

        # 6. Multi-source entity & geographic scope gate
        # Pass raw `1` total_floors if originally 1 so multi-room row houses are caught before nulling
        gate_total_floors = 1 if raw_total_floors_input == 1 else self.total_floors
        validate_entity_and_scope(
            is_active=self.is_active,
            title=self.title,
            description=self.description,
            city=self.city,
            district=self.district,
            street=self.street,
            latitude=self.latitude,
            longitude=self.longitude,
            price_eur=self.price_eur,
            price_display=self.price_display,
            area_sqm=self.area_sqm,
            floor=self.floor,
            total_floors=gate_total_floors,
            rooms_count=self.rooms_count,
            vlastnictvo=self.vlastnictvo,
            construction=self.construction,
            plocha_pozemku=self.plocha_pozemku,
            zastavana_plocha=self.zastavana_plocha,
            exclude_reserved=self.exclude_reserved,
        )

        # 7. Three-valued balcony resolution (`True` / `False` / `None`)
        self.has_balcony = resolve_balcony(
            attr_balcony=self.has_balcony,
            title=self.title,
            url=self.url,
            sections=sections,
        )

        # 8. Pure interior floor area (`Podlahová plocha`) recovery and decomposition
        min_area = MIN_AREA_BY_ROOMS.get(self.rooms_count or 1, 10.0)

        # Recover missing-zero typo (e.g. area=8 vs Zastavaná plocha=80)
        if (
            self.area_sqm is not None
            and self.area_sqm < 20.0
            and self.zastavana_plocha is not None
            and 25.0 <= self.zastavana_plocha <= 250.0
            and self.zastavana_plocha != self.plocha_pozemku
            and abs(self.zastavana_plocha - self.area_sqm * 10.0) <= 5.0
        ):
            self.area_sqm = self.zastavana_plocha

        # Fix missing-decimal typos (`392 -> 39.2`, `654 -> 65.4`) BEFORE the 600 m2 cutoff
        area_before_decimal_fix = self.area_sqm
        self.area_sqm = fix_missing_decimal_area(
            area_sqm=self.area_sqm,
            rooms_count=self.rooms_count,
            price_eur=self.price_eur,
            min_area=min_area,
        )

        if self.area_sqm is not None and not (min_area <= self.area_sqm <= 600.0):
            self.area_sqm = None
            self.price_per_sqm_eur = None

        exact_portal_area = (
            compute_exact_portal_area(self.area_sqm, self.price_eur, raw_portal_unit_price)
            if (self.area_sqm is not None and self.area_sqm == area_before_decimal_fix)
            else self.area_sqm
        )

        self.area_sqm = resolve_pure_apartment_area(
            raw_area=self.area_sqm,
            min_area=min_area,
            title=self.title,
            description=self.description,
            exterior_area=self.exterior_area,
            exact_portal_area=exact_portal_area,
        )

        # 9. Resolve property_condition & building elevator availability
        ref_year = self.updated_at.year if self.updated_at is not None else None
        is_modern_complex = is_modern_complex_title(self.title)

        self.property_condition = resolve_property_condition(
            raw_condition=self.property_condition,
            title=self.title,
            sections=sections,
            construction=self.construction,
            year_of_construction=self.year_of_construction,
            year_of_construction_lookup=self.year_of_construction_lookup,
            rok_kolaudacie=self.rok_kolaudacie,
            reference_year=ref_year,
        )

        self.has_elevator = resolve_elevator(
            raw_has_elevator=self.has_elevator,
            title=self.title,
            sections=sections,
            floor=self.floor,
            total_floors=self.total_floors,
            property_condition=self.property_condition,
            is_modern_complex=is_modern_complex,
        )

        # 10. Final price bounds, empty-shell guard, unit price recomputation, total_floors guard
        self._apply_price_bounds()

        title_clean = normalize_text(self.title)
        if (
            self.price_eur is None
            and self.area_sqm is None
            and not any(kw in title_clean for kw in (*APARTMENT_KEYWORDS, "izb", "byvani", "domov"))
        ):
            raise ValueError("Listing has neither price_eur nor area_sqm")

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

        if self.total_floors is not None and (
            self.total_floors <= 1
            or (self.floor is not None and self.floor > self.total_floors)
        ):
            self.total_floors = None

        return self
