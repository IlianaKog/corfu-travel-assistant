"""Structured conversation state, kept on the server.

The LLM changes state only through tool arguments. Each tool merges what it is
given over what is stored, so preferences established earlier survive unless
the visitor changes them, and every tool result echoes the active constraints
so the model can see what is still in force.
"""

import datetime as dt
import threading
import uuid
from datetime import datetime, time

from pydantic import BaseModel, Field

from app.itinerary import Itinerary, PlanConstraints
from app.models import Category, Setting


class Preferences(BaseModel):
    """Visitor preferences and constraints that persist across turns."""

    # dt.date: a field named `date` with a default would shadow the type
    date: dt.date | None = None
    start: time | None = None
    end: time | None = None
    interests: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    exclude_categories: list[Category] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    settings: list[Setting] = Field(default_factory=list)
    with_child: bool = False
    child_age: int | None = None
    excluded_poi_ids: list[str] = Field(default_factory=list)
    # Places the visitor asked for by name; later changes keep them while they still fit.
    named_places: list[str] = Field(default_factory=list)
    # Set once the plan has been checked against the weather, so later changes keep avoiding rain.
    consider_weather: bool = False
    # No stop count here: "five museums" belongs to one request, not to the visitor.

    def merged(self, **updates) -> "Preferences":
        """Return a copy with every non-None update applied."""
        return self.model_copy(update={k: v for k, v in updates.items() if v is not None})

    def to_constraints(self, **overrides) -> PlanConstraints:
        if self.date is None or self.start is None or self.end is None:
            missing = [n for n in ("date", "start", "end") if getattr(self, n) is None]
            raise ValueError(f"missing {', '.join(missing)}: ask the visitor, or pass it explicitly")
        data = self.model_dump(exclude_none=True, exclude={"consider_weather", "named_places"})
        data.update({k: v for k, v in overrides.items() if v is not None})
        return PlanConstraints(**data)

    def describe(self) -> dict:
        d = self.model_dump(mode="json", exclude_defaults=True)
        if self.date:
            d["weekday"] = f"{self.date:%A}"
        return d


class SessionState(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=datetime.now)
    preferences: Preferences = Field(default_factory=Preferences)
    itinerary: Itinerary | None = None
    weather_fixture: str | None = None  # path of an injected fixture; None = live weather
    messages: list[dict] = Field(default_factory=list)  # OpenAI-format chat history (no system prompt)


class SessionStore:
    """In-memory, process-local sessions. Lost on restart (documented limitation)."""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def create(self, weather_fixture: str | None = None) -> SessionState:
        s = SessionState(weather_fixture=weather_fixture)
        with self._lock:
            self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)
