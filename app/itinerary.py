"""Itinerary request, result and validation models."""

from datetime import date, datetime, time

from pydantic import BaseModel, Field, model_validator

from app import config
from app.models import Category, Setting


class PlanConstraints(BaseModel):
    """Everything the planner needs. Built from session state, never free text."""

    date: date
    start: time
    end: time
    interests: list[str] = Field(default_factory=list, description="free-text interest terms, e.g. 'venetian history'")
    categories: list[Category] = Field(default_factory=list, description="restrict to these categories; empty = sightseeing default")
    exclude_categories: list[Category] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list, description="e.g. ['museum'] to avoid museums")
    settings: list[Setting] = Field(default_factory=list, description="allowed settings; empty = any")
    with_child: bool = False
    child_age: int | None = None
    required_poi_ids: list[str] = Field(default_factory=list)
    excluded_poi_ids: list[str] = Field(default_factory=list)
    # Set only when the visitor asked for a number of stops ("five museums"); the
    # planner then explains a shortfall. Otherwise the default limit applies.
    max_stops: int | None = Field(default=None, ge=1, le=8)

    @model_validator(mode="after")
    def _check_window(self):
        minutes = self.window_minutes
        if minutes <= 0:
            raise ValueError(f"end time {self.end:%H:%M} must be after start time {self.start:%H:%M} on the same day")
        if not config.MIN_WINDOW_MIN <= minutes <= config.MAX_WINDOW_MIN:
            raise ValueError(
                f"time window of {minutes} min is outside the supported "
                f"{config.MIN_WINDOW_MIN}-{config.MAX_WINDOW_MIN} min range"
            )
        return self

    @property
    def window_minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)

    @property
    def stop_limit(self) -> int:
        return self.max_stops or config.MAX_STOPS

    @property
    def walk_speed_kmh(self) -> float:
        return config.WALK_SPEED_WITH_CHILD_KMH if self.with_child else config.WALK_SPEED_KMH


class Stop(BaseModel):
    poi_id: str
    name: str
    setting: Setting
    arrive: datetime
    start: datetime  # may be later than arrive when waiting for opening
    end: datetime
    walk_minutes: int = 0  # walking from the previous stop, excluding buffer
    walk_km: float = 0.0  # estimated route distance from the previous stop
    buffer_minutes: int = 0

    @property
    def visit_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def wait_minutes(self) -> int:
        return int((self.start - self.arrive).total_seconds() // 60)


class Itinerary(BaseModel):
    date: date
    window_start: datetime
    window_end: datetime
    stops: list[Stop]
    walk_speed_kmh: float

    @property
    def total_walk_minutes(self) -> int:
        return sum(s.walk_minutes for s in self.stops)

    @property
    def total_walk_km(self) -> float:
        return round(sum(s.walk_km for s in self.stops), 2)

    @property
    def finish(self) -> datetime | None:
        return self.stops[-1].end if self.stops else None

    @property
    def free_minutes(self) -> int:
        if not self.stops:
            return int((self.window_end - self.window_start).total_seconds() // 60)
        return int((self.window_end - self.stops[-1].end).total_seconds() // 60)

    def poi_ids(self) -> list[str]:
        return [s.poi_id for s in self.stops]


class Issue(BaseModel):
    code: str  # machine-readable, e.g. "closed", "not_walkable", "window_exceeded"
    message: str
    poi_id: str | None = None


class ValidationReport(BaseModel):
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)  # human-readable list of what was verified

    @property
    def ok(self) -> bool:
        return not self.errors


class PlanResult(BaseModel):
    """Planner output handed to the tool layer and, from there, to the LLM."""

    status: str  # "ok" | "partial" | "infeasible" | "unchanged"
    itinerary: Itinerary | None = None
    validation: ValidationReport | None = None
    notes: list[str] = Field(default_factory=list)  # explanations of decisions and failures
    changes: list[str] = Field(default_factory=list)  # what changed relative to a previous plan
