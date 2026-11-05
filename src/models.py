import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

MIN_FLOOR = -2
MAX_FLOOR = 46

ROOMS_BY_SUBCATEGORY: dict[str, int] = {
    "GARSONKA": 1,
    "DOUBLE_GARSONKA": 2,
    "ONE_ROOM_APARTMENT": 1,
    "TWO_ROOM_APARTMENT": 2,
    "THREE_ROOM_APARTMENT": 3,
    "FOUR_ROOM_APARTMENT": 4,
    "FIVE_AND_MORE_ROOM_APARTMENT": 5,
}

REJECT_TITLE_STEMS = (
    "rezervovan",
    "predan",
    "kupim",
    "hladam",
    "dopyt",
)


def parse_positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return round(float(value), 2) if value > 0 else None

    if isinstance(value, str):
        value = value.replace("\xa0", " ")
        value = re.sub(r"(?<=\d)\s+(?=\d)", "", value)
        value = value.replace(",", ".")

        match = re.search(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value)
        if match is None:
            return None

        number = float(match.group())
        return round(number, 2) if number > 0 else None

    return None


def parse_floor(value: Any) -> int | None:
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
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    value = re.sub(r"^okres\s+", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s*-\s*", "-", value)


def _remove_diacritics(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


class ApartmentModel(BaseModel):
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
    has_balcony: bool = False
    city: str | None = None
    district: str | None = None
    updated_at: datetime | None = None

    @field_validator("transaction_type", mode="before")
    @classmethod
    def normalize_transaction_type(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _remove_diacritics(value.strip()).casefold() or None

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
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_apartment_consistency(self) -> "ApartmentModel":
        # Ignore unavailable, reserved, sold, and wanted-ad listings.
        if self.title:
            title = _remove_diacritics(self.title).casefold()
            rejected_word = next(
                (stem for stem in REJECT_TITLE_STEMS if stem in title),
                None,
            )
            if rejected_word is not None:
                raise ValueError(
                    f"Apartment title contains a rejected stem: {rejected_word}"
                )

        if self.rooms_count is not None and not (1 <= self.rooms_count <= 20):
            self.rooms_count = None

        expected_rooms = ROOMS_BY_SUBCATEGORY.get(self.category_sub or "")
        if expected_rooms is not None:
            if self.category_sub == "FIVE_AND_MORE_ROOM_APARTMENT":
                if self.rooms_count is None or self.rooms_count < 5:
                    self.rooms_count = 5
            else:
                self.rooms_count = expected_rooms

        if self.area_sqm is not None and not (10.0 <= self.area_sqm <= 600.0):
            self.area_sqm = None
            self.price_per_sqm_eur = None

        if self.price_eur is not None and self.price_eur < 100:
            self.price_eur = None
            self.price_per_sqm_eur = None

        if self.city and self.city.casefold() == "slovensko":
            self.city = None

        if self.price_eur is not None and self.area_sqm is not None and self.area_sqm > 0:
            if self.price_eur > 0:
                self.price_per_sqm_eur = round(self.price_eur / self.area_sqm, 2)

        if self.total_floors is not None and (
                self.total_floors < 1
                or (self.floor is not None and self.floor > self.total_floors)
                or (self.has_elevator and self.total_floors == 1)
        ):
            self.total_floors = None

        return self
    