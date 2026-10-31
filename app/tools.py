"""Tools exposed to the LLM.

Each tool has a Pydantic argument model (validated before execution) and
returns compact JSON facts. Tools read and update SessionState; the LLM never
writes the itinerary itself.
"""

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from app import planner
from app.data import POIRepository, get_repository
from app.hours import local_dt, open_intervals
from app.itinerary import Itinerary, PlanConstraints, PlanResult
from app.models import POI, Category, Setting
from app.recommend import candidates, combine_scores, keyword_scores, split_setting_words
from app.state import Preferences, SessionState
from app.weather import WeatherProvider, WeatherUnavailable, now_local, provider_for, summarize

# Facts the dataset never contains. Returned with every search so the model
# can refuse questions that retrieval would otherwise "answer" with a near miss.
DATASET_GAPS = [
    "ticket prices, ticket availability or admission rules",
    "bookings, reservations or tours",
    "current/live opening hours (only exercise fixture hours exist)",
    "accessibility, safety or child-safety certifications",
    "event or performance schedules",
    "public transport, taxis, parking or routed travel times",
    "any place that is not one of the 50 dataset records",
]


# --- dependencies ------------------------------------------------------------------

@dataclass
class Deps:
    repo: POIRepository = field(default_factory=get_repository)
    index_factory: Callable[[], Any] | None = None  # lazily loads the vector index
    weather_factory: Callable[[str | None], WeatherProvider] = provider_for

    @property
    def index(self):
        if self.index_factory is None:
            from app.rag.index import get_index

            self.index_factory = get_index
        return self.index_factory()


@dataclass
class ToolOutput:
    data: dict
    itinerary_changed: bool = False
    sources: list[str] = field(default_factory=list)  # POI ids the result is based on
    weather_source: str | None = None
    notices: list[str] = field(default_factory=list)  # shown to the visitor verbatim by the app, not by the LLM


def ranking_terms(c: PlanConstraints) -> list[str]:
    """Interests for relevance ranking. Models often pass categories=['history']
    without interests; without a fallback the planner would fill the window with
    whatever history-category place is longest, regardless of fit."""
    return c.interests or [cat.value.replace("_", " ") for cat in c.categories]


def scores_for(deps: Deps, interests: list[str]) -> dict[str, float]:
    kw = keyword_scores(deps.repo.all(), interests)
    if not interests:
        return kw
    return combine_scores(kw, deps.index.semantic_scores("; ".join(interests)))


# Shared argument descriptions. Seen live: for "5 museums" the model sent only
# categories=["history"], so "museum" was lost and the planner picked the shortest
# history places (squares, gates) instead of museums.
INTERESTS_DOC = ("The visitor's topics in their own words, e.g. ['museums'], ['art'], ['venetian history', 'views']. "
                 "Always include them: they rank and filter places. Museums and galleries are not a category.")
CATEGORIES_DOC = ("Optional coarse dataset category (history, nature, food, local_market, nightlife). Too broad on its own: "
                  "'history' includes fortresses, squares, churches and museums. Use only together with interests.")


# --- argument models ------------------------------------------------------------------

def _tidy_time(value):
    """Normalise times to local HH:MM, forgiving formatting slips seen from real
    models in the eval ("17:00,", "10:00,00", "14:00:00.000Z", "9:00", "13.30").

    Tool times are Europe/Athens wall-clock by contract, so seconds and any
    timezone suffix are dropped explicitly ("14:00Z" means 14:00 local, as the
    visitor said it). Anything still not HH:MM is rejected as before."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0, tzinfo=None)
    if not isinstance(value, str):
        return value
    v = value.strip().rstrip(".,;").strip()
    v = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", v)  # timezone suffix
    v = re.sub(r"^(\d{1,2}:\d{2})[,:.]\d{2}(\.\d+)?$", r"\1", v)  # seconds / fractions
    v = re.sub(r"^(\d{1,2})\.(\d{2})$", r"\1:\2", v)
    if re.fullmatch(r"\d:\d{2}", v):
        v = "0" + v
    return v


LenientTime = Annotated[time, BeforeValidator(_tidy_time)]


class SearchArgs(BaseModel):
    query: str = Field(description="The visitor's question or topic, in English.")
    k: int = Field(default=4, ge=1, le=8, description="Number of records to return.")
    categories: list[Category] | None = Field(default=None, description="Optional category filter.")


class DetailsArgs(BaseModel):
    places: list[str] = Field(min_length=1, max_length=8, description="Place names as the visitor wrote them, or POI ids like CF-001.")
    date: dt.date | None = Field(default=None, description="Optional ISO date to report that day's fixture hours.")


class RecommendArgs(BaseModel):
    interests: list[str] | None = Field(default=None, description=INTERESTS_DOC)
    categories: list[Category] | None = Field(default=None, description=CATEGORIES_DOC)
    exclude_tags: list[str] | None = Field(default=None, description="Things to avoid, e.g. ['museum'].")
    settings: list[Setting] | None = Field(default=None, description="Allowed settings, e.g. ['outdoor'].")
    with_child: bool | None = None
    child_age: int | None = Field(default=None, ge=0, le=17)
    date: dt.date | None = Field(default=None, description="If given, only places open that day (fixture hours).")
    limit: int = Field(default=6, ge=1, le=12)


class WeatherArgs(BaseModel):
    date: dt.date | None = Field(default=None, description="ISO date. Omit together with times to use next_hours from now.")
    start_time: LenientTime | None = Field(default=None, description="HH:MM local time; default 09:00.")
    end_time: LenientTime | None = Field(default=None, description="HH:MM local time; default 21:00.")
    next_hours: int | None = Field(default=None, ge=1, le=72, description="Forecast window from now, e.g. 24.")


class PlanArgs(BaseModel):
    date: dt.date | None = Field(default=None, description="ISO date. Omit to keep the session's date.")
    start_time: LenientTime | None = Field(default=None, description="HH:MM local time.")
    end_time: LenientTime | None = Field(default=None, description="HH:MM local time. Or give duration_minutes.")
    duration_minutes: int | None = Field(default=None, ge=30, le=600, description="Alternative to end_time.")
    interests: list[str] | None = Field(default=None, description=INTERESTS_DOC + " Replaces earlier interests.")
    categories: list[Category] | None = Field(default=None, description=CATEGORIES_DOC)
    exclude_tags: list[str] | None = Field(default=None, description="Added to earlier exclusions, e.g. ['museum'].")
    exclude_categories: list[Category] | None = None
    settings: list[Setting] | None = Field(default=None, description="Allowed settings, e.g. ['outdoor'].")
    with_child: bool | None = None
    child_age: int | None = Field(default=None, ge=0, le=17)
    must_include_places: list[str] | None = Field(default=None, description="Specific places the visitor named; triggers a feasibility check.")
    max_stops: int | None = Field(default=None, ge=1, le=8, description="Number of stops the visitor asked for, e.g. 5 for 'five museums'. Applies to this request only.")
    consider_weather: bool = Field(default=False, description="Fetch the forecast and avoid outdoor stops during rain.")
    fresh_start: bool = Field(default=False, description="True only if the visitor asks for a new plan with different preferences; clears earlier preferences.")


class ReplaceArgs(BaseModel):
    stop_number: int = Field(ge=1, description="1-based position of the stop to replace.")
    interests: list[str] | None = Field(default=None, description="What the replacement should be, in the visitor's words, e.g. ['park'], ['museum'].")
    settings: list[Setting] | None = Field(default=None, description="Allowed settings for the replacement only.")
    categories: list[Category] | None = Field(default=None, description=CATEGORIES_DOC)
    exclude_tags: list[str] | None = None
    with_child: bool | None = Field(default=None, description="Persisted: the party includes a child.")
    child_age: int | None = Field(default=None, ge=0, le=17)


class ReviseArgs(BaseModel):
    date: dt.date | None = None
    start_time: LenientTime | None = None
    end_time: LenientTime | None = None
    interests: list[str] | None = Field(default=None, description=INTERESTS_DOC + " Replaces earlier interests.")
    categories: list[Category] | None = Field(default=None, description=CATEGORIES_DOC)
    exclude_tags: list[str] | None = Field(default=None, description="Added to earlier exclusions.")
    allow_tags: list[str] | None = Field(default=None, description="Remove earlier exclusions.")
    exclude_categories: list[Category] | None = None
    settings: list[Setting] | None = None
    with_child: bool | None = None
    child_age: int | None = Field(default=None, ge=0, le=17)
    remove_places: list[str] | None = Field(default=None, description="Places to drop from the plan.")
    max_stops: int | None = Field(default=None, ge=1, le=8)


class AdaptWeatherArgs(BaseModel):
    pass


# --- helpers ------------------------------------------------------------------------

def _resolve(repo: POIRepository, names: list[str]) -> tuple[list[POI], list[dict]]:
    found, problems = [], []
    for name in names:
        poi = repo.get(name.strip().upper()) or repo.resolve_name(name)
        if poi:
            found.append(poi)
            continue
        options = repo.match_name(name)
        if options:
            problems.append({"name": name, "problem": "ambiguous", "candidates": [f"{m.poi.name} ({m.poi.id})" for m in options[:3]]})
        else:
            problems.append({"name": name, "problem": "not in the dataset"})
    return found, problems


def _union(old: list, new: list | None) -> list:
    return list(dict.fromkeys([*old, *(new or [])]))


def _itinerary_json(itin: Itinerary | None) -> dict | None:
    if itin is None:
        return None
    return {
        "date": f"{itin.date.isoformat()} ({itin.date:%A})",
        "window": f"{itin.window_start:%H:%M}-{itin.window_end:%H:%M}",
        "stops": [
            {"number": i, "poi_id": s.poi_id, "name": s.name, "time": f"{s.start:%H:%M}-{s.end:%H:%M}", "setting": s.setting.value,
             "walk_from_previous_min": s.walk_minutes}
            for i, s in enumerate(itin.stops, 1)
        ],
        "finish": f"{itin.finish:%H:%M}" if itin.finish else None,
        "spare_minutes": itin.free_minutes,
    }


def _plan_output(state: SessionState, result: PlanResult, extra: dict | None = None) -> ToolOutput:
    changed = result.itinerary is not None and result.status != "infeasible" and result.itinerary is not state.itinerary
    if changed:
        state.itinerary = result.itinerary
    data = {
        "status": result.status,
        "itinerary": _itinerary_json(result.itinerary if result.status != "infeasible" else state.itinerary),
        "notes": result.notes,
        "changes": result.changes,
        "validation": (
            {"ok": result.validation.ok, "errors": [e.message for e in result.validation.errors], "warnings": [w.message for w in result.validation.warnings]}
            if result.validation else None
        ),
        "active_constraints": state.preferences.describe(),
        "display": "The app shows the full timetable to the visitor automatically. Do not rewrite it; summarise and explain notes/changes.",
        **(extra or {}),
    }
    return ToolOutput(data, itinerary_changed=changed, sources=result.itinerary.poi_ids() if result.itinerary else [])


def _add_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date.min, t) + timedelta(minutes=minutes)).time()


def _window(args_date, start, end, duration, prefs: Preferences) -> tuple[date | None, time | None, time | None]:
    """Resolve date/start/end from arguments, falling back to the session.
    A duration is converted to an end time in code, not by the LLM. Moving only
    the start keeps the window's length ("from 16:00" after a one-hour plan
    means 16:00-17:00), so the end is never left behind or invented."""
    d = args_date or prefs.date
    s = start or prefs.start
    if end:
        e = end
    elif duration and s:
        e = _add_minutes(s, duration)
    elif start and prefs.start and prefs.end and start != prefs.start:
        length = (datetime.combine(date.min, prefs.end) - datetime.combine(date.min, prefs.start)).seconds // 60
        e = _add_minutes(start, length)
    else:
        e = prefs.end
    return d, s, e


_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _user_text(state: SessionState) -> str:
    return " ".join(m["content"] for m in state.messages if m.get("role") == "user" and isinstance(m.get("content"), str)).lower()


def _time_stated(t: time, text: str) -> bool:
    """Is this clock time written in the text, as 15:00 / 15.00 / 3 pm / 3pm / noon?"""
    h, m = t.hour, t.minute
    h12, meridiem = (h % 12 or 12), ("p" if h >= 12 else "a")
    patterns = [rf"\b0?{h}[:.]{m:02d}\b", rf"\b{h12}{f'[:.]{m:02d}' if m else rf'(?:[:.]00)?'}\s*{meridiem}\.?\s*m\b"]
    if (h, m) == (12, 0):
        patterns.append(r"\b(noon|midday)\b")
    return any(re.search(p, text) for p in patterns)


def _duration_stated(minutes: int, text: str) -> bool:
    """Is this window length written in the text ("one hour", "four-hour", "90 minutes")?"""
    patterns = [rf"\b{minutes}\s*-?\s*(minutes?|mins?)\b"]
    if minutes % 60 == 0:
        n = minutes // 60
        for word in filter(None, (str(n), _NUMBER_WORDS.get(n))):
            patterns.append(rf"\b{word}[\s-]*(hours?|hrs?|h)\b")
        if n == 1:
            patterns.append(r"\ban hour\b")
    if minutes == 30:
        patterns.append(r"\bhalf an hour\b")
    if minutes == 90:
        patterns.append(r"\b(an hour and a half|one and a half hours?|1\.5\s*hours?)\b")
    if 180 <= minutes <= 300:
        patterns.append(r"\bhalf[\s-]day\b")
    return any(re.search(p, text) for p in patterns)


def _unstated_times(state: SessionState, c: PlanConstraints) -> list[str]:
    """Times the model used that the visitor never wrote. Seen live: given only a
    date, the model chose 15:00 and called it "your requested time"; a prompt rule
    to disclose assumptions was ignored, so the app discloses them itself."""
    text = _user_text(state)
    if not text:  # tools called directly (tests, scripts): no conversation to check
        return []
    now = now_local()
    said_now = re.search(r"\b(now|right now|right away|immediately)\b", text) and c.date == now.date() \
        and abs(local_dt(c.date, c.start) - now) <= timedelta(minutes=15)
    notices = []
    if not (_time_stated(c.start, text) or said_now):
        notices.append(f"The start time {c.start:%H:%M} was not stated in your messages and was assumed. "
                       "Please confirm it or give the time you prefer.")
    if not (_time_stated(c.end, text) or _duration_stated(c.window_minutes, text)):
        notices.append(f"The end time {c.end:%H:%M} ({c.window_minutes}-minute window) was not stated in your messages "
                       "and was assumed. Please confirm it or give the time you prefer.")
    return notices


def _with_time_notices(out: ToolOutput, state: SessionState, c: PlanConstraints) -> ToolOutput:
    notices = _unstated_times(state, c)
    if notices:
        out.notices += notices
        out.data["assumed_times"] = notices
        out.data["assumed_times_guidance"] = "The app shows these notices to the visitor. Do not describe these times as requested by the visitor."
    return out


PAST_START_TOLERANCE = timedelta(minutes=5)


def _start_time_check(c: PlanConstraints) -> dict:
    """For a plan on today's date, report the current Corfu time and flag a start
    time that has already passed. Seen live: asked for a date only, the model
    picked 14:30 itself. Past dates are not flagged: the brief's scenarios use them."""
    now = now_local()
    if c.date != now.date():
        return {}
    out = {"current_time_in_corfu": f"{now:%H:%M}"}
    if local_dt(c.date, c.start) < now - PAST_START_TOLERANCE:
        out["start_time_warning"] = (
            f"The start time {c.start:%H:%M} is already in the past (now {now:%H:%M} in Corfu). "
            "If the visitor did not give this time, say it was assumed and ask them to confirm."
        )
    return out


WEATHER_GUIDANCE = "State the source (live Open-Meteo or synthetic fixture) and the precipitation probability."


def forecast_facts(report, start, end) -> dict:
    """The forecast as every weather-reading tool returns it, so the model gets the same facts and guidance from each."""
    data = summarize(report, start, end)
    data["request_url"] = report.request_url
    data["guidance"] = WEATHER_GUIDANCE
    return data


def _rain_for(deps: Deps, state: SessionState, c: PlanConstraints) -> tuple[list, dict]:
    try:
        provider = deps.weather_factory(state.weather_fixture)
        start, end = local_dt(c.date, c.start), local_dt(c.date, c.end)
        report = provider.forecast(start, end)
        return report.rain_windows(), {"weather": forecast_facts(report, start, end)}
    except WeatherUnavailable as e:
        return [], {"weather": {"unavailable": str(e)}}


def _with_weather(deps: Deps, state: SessionState, c: PlanConstraints) -> tuple[PlanConstraints, list, dict]:
    """Rain windows for planner and validator once the plan has been checked
    against the weather. Seen in testing: after adapt_plan_to_weather, adding a
    child re-planned without the forecast and put outdoor stops back into the
    rain. As in the adaptation, indoor places are allowed when rain is forecast."""
    if not state.preferences.consider_weather:
        return c, [], {}
    rain, extra = _rain_for(deps, state, c)
    if rain and c.settings and Setting.INDOOR not in c.settings:
        c = c.model_copy(update={"settings": sorted({*c.settings, Setting.INDOOR}, key=lambda s: s.value)})
        extra["weather_note"] = "Indoor places are allowed for the rainy hours, relaxing the outdoor preference."
    return c, rain, extra


def _weather_output(out: ToolOutput, extra: dict) -> ToolOutput:
    if source := extra.get("weather", {}).get("source"):
        out.weather_source = source
    return out


# --- tools ---------------------------------------------------------------------------

def search_places_info(state: SessionState, args: SearchArgs, deps: Deps) -> ToolOutput:
    allowed = {p.id for p in deps.repo.all() if p.category in args.categories} if args.categories else None
    res = deps.index.search(args.query, k=args.k, allowed_ids=allowed)
    results = []
    for h in res.hits:
        poi = deps.repo.get(h.doc.poi_id)
        results.append({"poi_id": poi.id, "name": poi.name, "similarity": h.score, "category": poi.category.value,
                        "setting": poi.setting.value, "passage": h.doc.passage, "source_urls": h.doc.source_urls})
    return ToolOutput(
        {
            "results": results,
            "query_terms_not_found_in_dataset": res.unmatched_terms,
            "dataset_does_not_contain": DATASET_GAPS,
            "guidance": "Similarity is not proof. Answer only with facts a passage states, citing its poi_id. "
            "If no passage states it, say the dataset does not contain it.",
        },
        sources=[r["poi_id"] for r in results],
    )


def get_place_details(state: SessionState, args: DetailsArgs, deps: Deps) -> ToolOutput:
    found, problems = _resolve(deps.repo, args.places)
    out = []
    for p in found:
        item = {
            "poi_id": p.id, "name": p.name, "area": p.area, "category": p.category.value, "tags": p.tags,
            "setting": p.setting.value, "suggested_visit_minutes": p.suggested_visit_minutes,
            "opening_hours_fixture": p.opening_hours_fixture or "unknown (no confirmed opening window; do not promise access)",
            "family_suitability_fixture": p.family_suitability_fixture, "description": p.description, "source_urls": p.source_urls,
        }
        if args.date:
            intervals = open_intervals(p, args.date)
            item["on_date"] = (
                "unknown hours" if intervals is None else "closed (fixture)" if not intervals
                else ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in intervals) + " (fixture)"
            )
        out.append(item)
    return ToolOutput({"places": out, "unresolved": problems, "note": "Hours, durations and family flags are exercise fixtures, not live information."},
                      sources=[p.id for p in found])


def recommend_places(state: SessionState, args: RecommendArgs, deps: Deps) -> ToolOutput:
    prefs = state.preferences.merged(
        interests=args.interests, categories=args.categories, settings=args.settings,
        with_child=args.with_child, child_age=args.child_age,
    )
    prefs.exclude_tags = _union(prefs.exclude_tags, args.exclude_tags)
    state.preferences = prefs
    day = args.date or prefs.date or now_local().date()
    c = PlanConstraints(date=day, start=time(9), end=time(18), interests=prefs.interests, categories=prefs.categories,
                        exclude_tags=prefs.exclude_tags, exclude_categories=prefs.exclude_categories, settings=prefs.settings,
                        with_child=prefs.with_child)
    scores = scores_for(deps, ranking_terms(c))
    ranked = sorted(candidates(deps.repo.all(), c, scores), key=lambda p: (-scores.get(p.id, 0), p.id))
    out = []
    for p in ranked:
        intervals = open_intervals(p, day) if args.date else True
        if args.date and not intervals:
            continue
        out.append({"poi_id": p.id, "name": p.name, "relevance": round(scores.get(p.id, 0), 2), "category": p.category.value,
                    "tags": p.tags, "setting": p.setting.value, "family_suitability_fixture": p.family_suitability_fixture,
                    "hours_known": p.hours_known})
        if len(out) == args.limit:
            break
    return ToolOutput({"recommendations": out, "active_constraints": prefs.describe(),
                       "filtered_by_open_on": day.isoformat() if args.date else None}, sources=[r["poi_id"] for r in out])


def get_weather(state: SessionState, args: WeatherArgs, deps: Deps) -> ToolOutput:
    if args.date is None:
        start = now_local().replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=args.next_hours or 24)
    else:
        start, end = local_dt(args.date, args.start_time or time(9)), local_dt(args.date, args.end_time or time(21))
        if end <= start:
            return ToolOutput({"error": "end_time must be after start_time"})
    try:
        report = deps.weather_factory(state.weather_fixture).forecast(start, end)
    except WeatherUnavailable as e:
        return ToolOutput({"unavailable": str(e), "guidance": "Tell the visitor no forecast is available; do not guess."})
    return ToolOutput(forecast_facts(report, start, end), weather_source=report.source)


def plan_itinerary(state: SessionState, args: PlanArgs, deps: Deps) -> ToolOutput:
    base = Preferences() if args.fresh_start else state.preferences
    d, s, e = _window(args.date, args.start_time, args.end_time, args.duration_minutes, base)
    prefs = base.merged(date=d, start=s, end=e, interests=args.interests, categories=args.categories, settings=args.settings,
                        with_child=args.with_child, child_age=args.child_age, consider_weather=args.consider_weather or None)
    prefs.exclude_tags = _union(prefs.exclude_tags, args.exclude_tags)
    prefs.exclude_categories = _union(prefs.exclude_categories, args.exclude_categories)

    required: list[str] = []
    if args.must_include_places:
        found, problems = _resolve(deps.repo, args.must_include_places)
        if problems:
            return ToolOutput({"status": "needs_clarification", "unresolved": problems,
                               "guidance": "Tell the visitor which names are not in the dataset (or are ambiguous). Do not substitute or invent places."})
        required = [p.id for p in found]

    try:
        c = prefs.to_constraints(required_poi_ids=required or None, max_stops=args.max_stops)
    except ValueError as err:
        return ToolOutput({"error": str(err)})
    state.preferences = prefs

    c, rain, extra = _with_weather(deps, state, c)
    extra.update(_start_time_check(c))
    result = planner.plan(c, deps.repo, scores_for(deps, ranking_terms(c)), rain)
    out = _weather_output(_plan_output(state, result, extra), extra)
    return _with_time_notices(out, state, c)


def replace_stop(state: SessionState, args: ReplaceArgs, deps: Deps) -> ToolOutput:
    if state.itinerary is None:
        return ToolOutput({"error": "There is no itinerary yet. Create one with plan_itinerary first."})
    # party facts persist; slot-specific wishes apply to this replacement only
    state.preferences = state.preferences.merged(with_child=args.with_child, child_age=args.child_age)
    state.preferences.exclude_tags = _union(state.preferences.exclude_tags, args.exclude_tags)
    prefs = state.preferences
    try:
        c = prefs.to_constraints(interests=args.interests, settings=args.settings, categories=args.categories)
    except ValueError as err:
        return ToolOutput({"error": str(err)})
    c, rain, extra = _with_weather(deps, state, c)
    scores = scores_for(deps, ranking_terms(c))
    result = planner.replace_stop(state.itinerary, args.stop_number - 1, c, deps.repo, scores, rain)
    return _weather_output(_plan_output(state, result, extra), extra)


def revise_itinerary(state: SessionState, args: ReviseArgs, deps: Deps) -> ToolOutput:
    if state.itinerary is None:
        return ToolOutput({"error": "There is no itinerary yet. Create one with plan_itinerary first."})
    d, s, e = _window(args.date, args.start_time, args.end_time, None, state.preferences)
    prefs = state.preferences.merged(date=d, start=s, end=e, interests=args.interests,
                                     categories=args.categories, settings=args.settings, with_child=args.with_child,
                                     child_age=args.child_age)
    prefs.exclude_tags = [t for t in _union(prefs.exclude_tags, args.exclude_tags) if t not in set(args.allow_tags or [])]
    prefs.exclude_categories = _union(prefs.exclude_categories, args.exclude_categories)
    if args.remove_places:
        found, problems = _resolve(deps.repo, args.remove_places)
        if problems:
            return ToolOutput({"status": "needs_clarification", "unresolved": problems})
        prefs.excluded_poi_ids = _union(prefs.excluded_poi_ids, [p.id for p in found])
    try:
        c = prefs.to_constraints(max_stops=args.max_stops)
    except ValueError as err:
        return ToolOutput({"error": str(err)})
    state.preferences = prefs
    c, rain, extra = _with_weather(deps, state, c)
    result = planner.replan_preserving(state.itinerary, c, deps.repo, scores_for(deps, ranking_terms(c)), rain)
    return _with_time_notices(_weather_output(_plan_output(state, result, extra), extra), state, c)


def adapt_plan_to_weather(state: SessionState, args: AdaptWeatherArgs, deps: Deps) -> ToolOutput:
    if state.itinerary is None:
        return ToolOutput({"error": "There is no itinerary to adapt. Create one with plan_itinerary first."})
    try:
        c = state.preferences.to_constraints()
    except ValueError as err:
        return ToolOutput({"error": str(err)})
    try:
        report = deps.weather_factory(state.weather_fixture).forecast(state.itinerary.window_start, state.itinerary.window_end)
    except WeatherUnavailable as e:
        return ToolOutput({"status": "weather_unavailable", "reason": str(e), "itinerary": _itinerary_json(state.itinerary),
                           "guidance": "Say the forecast is unavailable; keep the plan; do not guess the weather."})
    weather = forecast_facts(report, state.itinerary.window_start, state.itinerary.window_end)
    state.preferences.consider_weather = True
    result = planner.adapt_to_weather(state.itinerary, c, deps.repo, scores_for(deps, ranking_terms(c)), report.rain_windows())
    out = _plan_output(state, result, {"weather": weather})
    out.weather_source = report.source
    return out


# --- registry ------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[SessionState, Any, Deps], ToolOutput]


TOOLS = [
    Tool("search_places_info", "Retrieve dataset passages to answer descriptive questions about places (history, what is there). Returns evidence with poi_ids.", SearchArgs, search_places_info),
    Tool("get_place_details", "Look up specific places by name or id: fixture opening hours, setting, duration, family flag, description.", DetailsArgs, get_place_details),
    Tool("recommend_places", "Rank places for the visitor's interests and constraints using structured filters (no itinerary).", RecommendArgs, recommend_places),
    Tool("get_weather", "Weather forecast for Corfu: a date and time window, or the next N hours from now. "
         "For any weather question except adapting an existing itinerary, which adapt_plan_to_weather does alone.", WeatherArgs, get_weather),
    Tool("plan_itinerary", "Create a new validated walking itinerary, or check whether named places fit a time window.", PlanArgs, plan_itinerary),
    Tool("replace_stop", "Replace one stop of the current itinerary, keeping the other stops and the time window.", ReplaceArgs, replace_stop),
    Tool("revise_itinerary", "Change constraints of the current itinerary (time window, exclusions, child, interests, remove places), keeping stops that still fit.", ReviseArgs, revise_itinerary),
    Tool("adapt_plan_to_weather", "Check the forecast for the current itinerary's date and window and adapt it to rain if needed, keeping the window. "
         "It fetches the forecast itself and returns it with the plan; do not call get_weather first.", AdaptWeatherArgs, adapt_plan_to_weather),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def openai_tool_specs() -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": clean_schema(t.args_model.model_json_schema())}}
        for t in TOOLS
    ]


def clean_schema(schema: dict) -> dict:
    """Inline $refs and turn Optional (anyOf [X, null]) into plain X, since
    OpenAI-compatible endpoints (e.g. Gemini) accept only a JSON Schema subset."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                merged = {**walk(options[0]), **{k: v for k, v in node.items() if k not in ("anyOf", "default")}} if len(options) == 1 else {"anyOf": [walk(o) for o in options]}
                return merged
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out = walk(schema)
    out.setdefault("properties", {})
    out["type"] = "object"
    return out


def run_tool(name: str, raw_args: str | dict, state: SessionState, deps: Deps) -> ToolOutput:
    """Validate arguments and execute. Errors are returned as data so the model can correct itself."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return ToolOutput({"error": f"unknown tool '{name}'", "available": list(TOOLS_BY_NAME)})
    try:
        payload = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        args = tool.args_model.model_validate(payload or {})
    except (json.JSONDecodeError, ValidationError) as e:
        detail = e.errors(include_url=False) if isinstance(e, ValidationError) else str(e)
        return ToolOutput({"error": "invalid arguments", "detail": json.loads(json.dumps(detail, default=str))})
    if "interests" in type(args).model_fields and "settings" in type(args).model_fields:
        interests, implied = split_setting_words(args.interests)
        if implied:
            args = args.model_copy(update={"interests": interests, "settings": list(dict.fromkeys([*(args.settings or []), *implied]))})
    try:
        return tool.fn(state, args, deps)
    except ValueError as e:  # constraint validation (e.g. window) surfaced from the domain layer
        return ToolOutput({"error": str(e)})
