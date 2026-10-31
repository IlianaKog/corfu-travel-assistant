"""Typed domain models for the POI dataset."""

from enum import Enum

from pydantic import BaseModel, Field

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Setting(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"


class Category(str, Enum):
    HISTORY = "history"
    NATURE = "nature"
    FOOD = "food"
    LOCAL_MARKET = "local_market"
    NIGHTLIFE = "nightlife"


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class POI(BaseModel):
    id: str
    name: str
    area: str
    category: Category
    tags: list[str]
    location: Location
    description: str
    setting: Setting
    suggested_visit_minutes: int = Field(gt=0)
    # None for the whole field = no confirmed opening window (unknown, NOT open).
    # None for a single weekday = closed that day.
    opening_hours_fixture: dict[str, str | None] | None
    source_urls: list[str] = []
    family_suitability_fixture: bool | None = None
    description_provenance: str | None = None

    @property
    def hours_known(self) -> bool:
        return self.opening_hours_fixture is not None

    @property
    def is_sheltered(self) -> bool:
        """Only fully indoor places count as rain shelter; 'mixed' does not."""
        return self.setting is Setting.INDOOR
