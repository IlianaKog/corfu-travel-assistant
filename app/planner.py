"""Deterministic itinerary planning, revision and feasibility checks.

All schedule arithmetic lives here. The LLM never computes times: it supplies
constraints, and everything returned is checked again by `validator.validate`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations, permutations

from app import config
from app.data import POIRepository
from app.geo import walk_leg
from app.hours import check_visit, earliest_visit_start, local_dt, open_intervals
from app.itinerary import Itinerary, PlanConstraints, PlanResult, Stop
from app.models import POI, Setting
from app.recommend import MIN_RELEVANCE, candidates, constraint_violation
from app.validator import validate
from app.weather import RainWindows, overlaps

# Objective weights: relevance dominates; longer visits fill the window;
# walking and waiting are penalised; kept stops are preferred during revisions.
W_RELEVANCE = 10.0
W_VISIT_MIN = 0.08
W_WALK_MIN = 0.15
W_WAIT_MIN = 0.10
W_KEEP = 4.0
BEAM_WIDTH = 60
MAX_REQUIRED_STOPS = 7


@dataclass(frozen=True)
class Context:
    day: date
    window_start: datetime
    window_end: datetime
    speed_kmh: float
    rain: RainWindows = field(default_factory=list)
    max_wait_min: int = config.MAX_WAIT_MIN

    @classmethod
    def from_constraints(cls, c: PlanConstraints, rain: RainWindows | None = None, reserve: bool = False) -> "Context":
        end = local_dt(c.date, c.end)
        if reserve and c.window_minutes >= config.PLAN_RESERVE_MIN_WINDOW:
            end -= timedelta(minutes=config.PLAN_RESERVE_MIN)
        return cls(
            day=c.date,
            window_start=local_dt(c.date, c.start),
            window_end=end,
            speed_kmh=c.walk_speed_kmh,
            rain=list(rain or []),
        )


@dataclass(frozen=True)
class Step:
    stop: Stop | None = None
    code: str | None = None
    reason: str | None = None


def try_stop(poi: POI, prev: Stop | None, prev_poi: POI | None, ctx: Context) -> Step:
    """Schedule `poi` right after `prev` (or at the window start), or explain why not."""
    if prev is None or prev_poi is None:
        arrive, walk_min, walk_km, buffer = ctx.window_start, 0, 0.0, 0
    else:
        leg = walk_leg(prev_poi.location, poi.location, speed_kmh=ctx.speed_kmh)
        if not leg.walkable:
            return Step(
                code="not_walkable",
                reason=f"{poi.name} is ~{leg.route_km:.1f} km on foot from {prev_poi.name}, "
                f"beyond the {config.MAX_WALK_LEG_KM:g} km walking limit",
            )
        walk_min, walk_km, buffer = leg.minutes, leg.route_km, config.TRANSITION_BUFFER_MIN
        arrive = prev.end + timedelta(minutes=walk_min + buffer)

    minutes = poi.suggested_visit_minutes
    if not poi.hours_known:
        return Step(code="unknown_hours", reason=f"{poi.name} has no confirmed opening hours in the dataset, so it cannot be scheduled")
    start = earliest_visit_start(poi, arrive, minutes)
    if start is None:
        return Step(code="closed", reason=check_visit(poi, arrive, minutes).reason)
    if start - arrive > timedelta(minutes=ctx.max_wait_min):
        return Step(code="wait_too_long", reason=f"{poi.name} would only open at {start:%H:%M}, too long a wait after arriving at {arrive:%H:%M}")
    end = start + timedelta(minutes=minutes)
    if end > ctx.window_end:
        return Step(
            code="window_exceeded",
            reason=f"{poi.name} ({minutes} min) would end at {end:%H:%M}, after the {ctx.window_end:%H:%M} end of the time window",
        )
    if not poi.is_sheltered and overlaps(ctx.rain, start, end):
        return Step(
            code="rain_exposed",
            reason=f"{poi.name} is {poi.setting.value} and would be visited {start:%H:%M}-{end:%H:%M} during forecast rain",
        )
    return Step(
        stop=Stop(
            poi_id=poi.id, name=poi.name, setting=poi.setting, arrive=arrive, start=start, end=end,
            walk_minutes=walk_min, walk_km=walk_km, buffer_minutes=buffer,
        )
    )


def schedule(pois: list[POI], ctx: Context) -> tuple[list[Stop], Step | None]:
    """Schedule a fixed sequence. Returns (stops so far, failing step or None)."""
    stops: list[Stop] = []
    for i, poi in enumerate(pois):
        step = try_stop(poi, stops[-1] if stops else None, pois[i - 1] if i else None, ctx)
        if step.stop is None:
            return stops, step
        stops.append(step.stop)
    return stops, None


def value(stops: list[Stop], scores: Mapping[str, float], keep: set[str] = frozenset()) -> float:
    v = 0.0
    for s in stops:
        v += W_RELEVANCE * scores.get(s.poi_id, 0.0) + W_VISIT_MIN * s.visit_minutes
        v -= W_WALK_MIN * s.walk_minutes + W_WAIT_MIN * s.wait_minutes
        if s.poi_id in keep:
            v += W_KEEP
    return v


@dataclass(frozen=True)
class _Node:
    pois: tuple[POI, ...]
    stops: tuple[Stop, ...]
    value: float


def beam_search(
    pool: list[POI],
    scores: Mapping[str, float],
    ctx: Context,
    max_stops: int,
    prefix: list[POI] | None = None,
    keep: set[str] = frozenset(),
) -> _Node:
    prefix = prefix or []
    prefix_stops, failed = schedule(prefix, ctx)
    if failed:
        raise ValueError(f"prefix cannot be scheduled: {failed.reason}")
    root = _Node(tuple(prefix), tuple(prefix_stops), value(prefix_stops, scores, keep))
    best, frontier = root, [root]
    while frontier:
        expanded: dict[tuple, _Node] = {}
        for node in frontier:
            if len(node.pois) >= max_stops:
                continue
            used = {p.id for p in node.pois}
            last_stop = node.stops[-1] if node.stops else None
            last_poi = node.pois[-1] if node.pois else None
            for poi in pool:
                if poi.id in used:
                    continue
                step = try_stop(poi, last_stop, last_poi, ctx)
                if step.stop is None:
                    continue
                stops = node.stops + (step.stop,)
                child = _Node(node.pois + (poi,), stops, value(list(stops), scores, keep))
                key = (frozenset(used | {poi.id}), poi.id)
                if key not in expanded or child.value > expanded[key].value:
                    expanded[key] = child
        frontier = sorted(expanded.values(), key=lambda n: (-n.value, [p.id for p in n.pois]))[:BEAM_WIDTH]
        if frontier and frontier[0].value > best.value:
            best = frontier[0]
    return best


def _itinerary(c: PlanConstraints, stops: list[Stop] | tuple[Stop, ...]) -> Itinerary:
    ctx = Context.from_constraints(c)
    return Itinerary(
        date=c.date, window_start=ctx.window_start, window_end=ctx.window_end,
        stops=list(stops), walk_speed_kmh=c.walk_speed_kmh,
    )


def _closed_relevant_notes(pool: list[POI], plan_ids: set[str], scores: Mapping[str, float], ctx: Context, limit: int = 5) -> list[str]:
    """Explain why highly relevant places are missing (closed / unknown hours)."""
    notes = []
    for poi in sorted(pool, key=lambda p: -scores.get(p.id, 0.0)):
        if poi.id in plan_ids or scores.get(poi.id, 0.0) < 0.5:
            continue
        intervals = open_intervals(poi, ctx.day)
        if intervals is None:
            notes.append(f"{poi.name} ({poi.id}) was not scheduled: no confirmed opening hours in the dataset.")
        elif not intervals:
            notes.append(f"{poi.name} ({poi.id}) is closed on {ctx.day:%A} (exercise fixture).")
        if len(notes) >= limit:
            break
    return notes


def _shortfall_notes(c: PlanConstraints, schedulable: list[POI], planned: int, ctx: Context) -> list[str]:
    """When the visitor asked for N stops and fewer fit, explain it with numbers,
    so the model relays a computed reason instead of inventing one.

    Uses a lower bound: the N shortest matching places open that day plus the
    transitions between them, before any walking."""
    n = c.max_stops
    if n is None or planned >= n:
        return []
    open_today = sorted((p for p in schedulable if open_intervals(p, ctx.day)), key=lambda p: p.suggested_visit_minutes)
    notes = [f"Only {planned} of the {n} requested stops fit in the {c.window_minutes}-min window ({c.start:%H:%M}-{c.end:%H:%M})."]
    if len(open_today) < n:
        notes.append(f"Only {len(open_today)} matching places with confirmed hours are open on {ctx.day:%A}.")
    shortest = open_today[:n]
    if shortest:
        visits = sum(p.suggested_visit_minutes for p in shortest)
        lower = visits + (len(shortest) - 1) * config.TRANSITION_BUFFER_MIN
        durations = ", ".join(f"{p.name} {p.suggested_visit_minutes}" for p in shortest)
        if lower > c.window_minutes:
            notes.append(f"Even the {len(shortest)} shortest matching visits need at least {lower} min "
                         f"({visits} min of visits plus {config.TRANSITION_BUFFER_MIN}-min transitions, before walking): {durations} min.")
        else:
            notes.append("Their visit times alone would fit; opening hours and walking distances between them leave room for fewer.")
    return notes


def plan(c: PlanConstraints, repo: POIRepository, scores: Mapping[str, float], rain: RainWindows | None = None) -> PlanResult:
    if c.required_poi_ids:
        return check_feasibility(c, repo, scores, rain)
    ctx = Context.from_constraints(c, rain, reserve=True)
    pool = candidates(repo.all(), c, scores)
    schedulable = [p for p in pool if p.hours_known]
    best = beam_search(schedulable, scores, ctx, c.stop_limit)
    if not best.stops:  # the reserve must never be the reason a plan is impossible
        ctx = Context.from_constraints(c, rain)
        best = beam_search(schedulable, scores, ctx, c.stop_limit)
    shortfall = _shortfall_notes(c, schedulable, len(best.stops), ctx)
    notes = shortfall + _closed_relevant_notes(pool, {p.id for p in best.pois}, scores, ctx)
    if not best.stops:
        return PlanResult(
            status="infeasible",
            notes=["No place in the dataset matches these constraints and is open long enough within the time window.", *notes],
        )
    itin = _itinerary(c, best.stops)
    status = "partial" if shortfall else "ok"
    return PlanResult(status=status, itinerary=itin, validation=validate(itin, c, repo, rain), notes=notes)


def replace_stop(
    itin: Itinerary,
    index: int,
    c: PlanConstraints,
    repo: POIRepository,
    scores: Mapping[str, float],
    rain: RainWindows | None = None,
) -> PlanResult:
    """Replace stop `index` (0-based), keeping the other stops and their order.

    `c` holds the constraints for the replacement; kept stops are not re-filtered
    (the visitor asked to keep them), but the whole plan is re-timed and validated.
    """
    if not 0 <= index < len(itin.stops):
        return PlanResult(status="infeasible", notes=[f"The itinerary has {len(itin.stops)} stops; there is no stop {index + 1}."])
    ctx = Context.from_constraints(c, rain)
    current = [repo.get(i) for i in itin.poi_ids()]
    removed, others = current[index], current[:index] + current[index + 1 :]
    used = {p.id for p in current}

    def search(pool: list[POI]):
        best = None
        for position in [index] + [i for i in range(len(others) + 1) if i != index]:
            for cand in pool:
                stops, failed = schedule(others[:position] + [cand] + others[position:], ctx)
                if failed:
                    continue
                v = value(stops, scores)
                if best is None or v > best[0]:
                    best = (v, stops, cand, position)
            if best:  # prefer the requested position whenever anything fits there
                return best
        return None

    def pool(min_relevance: float) -> list[POI]:
        return [p for p in candidates(repo.all(), c, scores, min_relevance) if p.hours_known and p.id not in used]

    notes = []
    best = search(pool(MIN_RELEVANCE))
    if best is None and c.interests:
        # Nothing matching the stated wish fits: fall back to any place that meets
        # the hard constraints (child, setting, hours, window), and say so.
        best = search(pool(0.0))
        if best:
            notes.append(f"No place matching '{', '.join(c.interests)}' fits the remaining time; "
                         f"{best[2].name} is the closest alternative that meets the other constraints.")

    if best is None:
        return PlanResult(status="infeasible", itinerary=itin, notes=_replacement_diagnosis(removed, others, pool(0.0), c, ctx))
    _, stops, cand, position = best
    new = _itinerary(c, stops)
    changes = [f"Replaced stop {index + 1} ({removed.name}) with {cand.name} ({cand.id})."]
    if position != index:
        changes.append(f"{cand.name} only fits as stop {position + 1}, so the order changed.")
    changes += _retime_changes(itin, new)
    return PlanResult(status="ok", itinerary=new, validation=validate(new, c, repo, rain, exempt=set(o.id for o in others)), notes=notes, changes=changes)


def _replacement_diagnosis(removed: POI, others: list[POI], pool: list[POI], c: PlanConstraints, ctx: Context) -> list[str]:
    """Explain a failed replacement with numbers instead of a generic refusal."""
    notes = [f"No replacement for {removed.name} fits while keeping the other stops and finishing by {c.end:%H:%M}. The itinerary is unchanged."]
    if constraint_violation(removed, c) is None:
        notes.append(f"{removed.name} [{removed.id}] itself already meets the new constraints"
                     + (" (flagged family-suitable in the dataset)" if c.with_child else "") + ", so keeping it is an option.")
    kept, failed = schedule(others, ctx)
    if failed:
        notes.append(f"Even without that stop the rest no longer fits: {failed.reason}.")
    elif kept:
        spare = int((ctx.window_end - kept[-1].end).total_seconds() // 60)
        pace = f" at a child's walking pace ({ctx.speed_kmh:g} km/h)" if c.with_child else ""
        notes.append(f"Without it, the other stops end at {kept[-1].end:%H:%M}{pace}, leaving {spare} min.")
        if pool:
            shortest = min(pool, key=lambda p: p.suggested_visit_minutes)
            notes.append(f"The shortest suitable alternative, {shortest.name} [{shortest.id}], needs {shortest.suggested_visit_minutes} min "
                         f"plus walking and a {config.TRANSITION_BUFFER_MIN}-min transition.")
    if not pool:
        notes.append("No place in the dataset meets the new constraints and has confirmed opening hours.")
    notes.append("Options: keep the current stop, drop another stop to make room, or extend the end time.")
    return notes


def adapt_to_weather(
    itin: Itinerary, c: PlanConstraints, repo: POIRepository, scores: Mapping[str, float], rain: RainWindows
) -> PlanResult:
    """Revise a plan so no exposed (outdoor/mixed) stop falls in forecast rain,
    keeping the original time window. Reorders first; replaces only if needed."""
    ctx = Context.from_constraints(c, rain)
    current = [repo.get(i) for i in itin.poi_ids()]
    exposed = [s for s in itin.stops if s.setting is not Setting.INDOOR and overlaps(rain, s.start, s.end)]
    if not exposed:
        return PlanResult(
            status="unchanged", itinerary=itin, validation=validate(itin, c, repo, rain),
            notes=["No outdoor or partly outdoor stop is scheduled during forecast rain; the plan stays as it is."],
        )
    keep = set(itin.poi_ids())
    notes = [f"{s.name} ({s.setting.value}) was scheduled {s.start:%H:%M}-{s.end:%H:%M}, during forecast rain." for s in exposed]

    # 1. Reorder the same stops so exposed visits fall in dry hours.
    if len(current) <= 6:
        best_order = None
        for order in permutations(current):
            stops, failed = schedule(list(order), ctx)
            if not failed:
                v = value(stops, scores, keep)
                if best_order is None or v > best_order[0]:
                    best_order = (v, stops)
        if best_order:
            new = _itinerary(c, best_order[1])
            return PlanResult(
                status="ok", itinerary=new, validation=validate(new, c, repo, rain), notes=notes,
                changes=["Reordered the same stops so outdoor visits happen before the rain.", *_retime_changes(itin, new)],
            )

    # 2. Keep the dry prefix, re-plan the rest; indoor places become eligible.
    relaxed = c.model_copy(update={"settings": sorted(set(c.settings) | {Setting.INDOOR}, key=lambda s: s.value)}) if c.settings else c
    if c.settings and Setting.INDOOR not in c.settings:
        notes.append("Indoor places were allowed for the rainy hours, relaxing the outdoor preference.")
    prefix_stops, _ = schedule(current, ctx)
    prefix = current[: len(prefix_stops)]
    pool = [p for p in candidates(repo.all(), relaxed, scores) if p.hours_known]
    pool += [p for p in current if p not in pool]
    best = beam_search(pool, scores, ctx, c.stop_limit, prefix=prefix, keep=keep)
    new = _itinerary(c, best.stops)
    old_ids, new_ids = set(itin.poi_ids()), set(new.poi_ids())
    changes = [f"Removed {repo.get(i).name} ({i})." for i in itin.poi_ids() if i not in new_ids]
    changes += [f"Added {s.name} ({s.poi_id}), {s.setting.value}." for s in new.stops if s.poi_id not in old_ids]
    changes += _retime_changes(itin, new)
    return PlanResult(status="ok" if new.stops else "infeasible", itinerary=new, validation=validate(new, relaxed, repo, rain), notes=notes, changes=changes)


def replan_preserving(
    itin: Itinerary, c: PlanConstraints, repo: POIRepository, scores: Mapping[str, float], rain: RainWindows | None = None
) -> PlanResult:
    """Apply changed constraints (window, exclusions, child, interests) to an
    existing plan, keeping the stops that still satisfy them where possible."""
    ctx = Context.from_constraints(c, rain)
    current = [repo.get(i) for i in itin.poi_ids()]
    still_allowed = [p for p in current if constraint_violation(p, c) is None and (not c.categories or p.category in c.categories)]
    notes = [f"{p.name} no longer fits the constraints: {constraint_violation(p, c) or 'category not requested'}." for p in current if p not in still_allowed]

    if still_allowed == current and c.date == itin.date:
        stops, failed = schedule(current, ctx)
        if not failed:
            new = _itinerary(c, stops)
            return PlanResult(status="ok", itinerary=new, validation=validate(new, c, repo, rain), changes=["Kept all stops.", *_retime_changes(itin, new)])
        notes.append(f"The same stops no longer fit: {failed.reason}.")

    pool = [p for p in candidates(repo.all(), c, scores) if p.hours_known]
    pool += [p for p in still_allowed if p not in pool]
    keep = {p.id for p in still_allowed}
    best = beam_search(pool, scores, Context.from_constraints(c, rain, reserve=True), c.stop_limit, keep=keep)
    if not best.stops:
        best = beam_search(pool, scores, ctx, c.stop_limit, keep=keep)
    if not best.stops:
        return PlanResult(status="infeasible", itinerary=itin, notes=[*notes, "Nothing fits the new constraints; the previous itinerary is unchanged."])
    new = _itinerary(c, best.stops)
    old_ids, new_ids = set(itin.poi_ids()), set(new.poi_ids())
    changes = [f"Kept {repo.get(i).name}." for i in itin.poi_ids() if i in new_ids]
    changes += [f"Removed {repo.get(i).name} ({i})." for i in itin.poi_ids() if i not in new_ids]
    changes += [f"Added {s.name} ({s.poi_id})." for s in new.stops if s.poi_id not in old_ids]
    return PlanResult(status="ok", itinerary=new, validation=validate(new, c, repo, rain), notes=notes, changes=changes + _retime_changes(itin, new))


def check_feasibility(
    c: PlanConstraints, repo: POIRepository, scores: Mapping[str, float], rain: RainWindows | None = None
) -> PlanResult:
    """Can all `required_poi_ids` be visited in the window? If not, say why and
    return the largest subset that fits."""
    ctx = Context.from_constraints(c, rain)
    unknown_ids = [i for i in c.required_poi_ids if repo.get(i) is None]
    if unknown_ids:
        return PlanResult(status="infeasible", notes=[f"Not in the dataset: {', '.join(unknown_ids)}. I can only plan with listed places."])
    required = [repo.get(i) for i in dict.fromkeys(c.required_poi_ids)]
    if len(required) > MAX_REQUIRED_STOPS:
        return PlanResult(status="infeasible", notes=[f"I can check at most {MAX_REQUIRED_STOPS} named places at once."])

    notes = []
    visit_total = sum(p.suggested_visit_minutes for p in required)
    window = c.window_minutes
    notes.append(f"The suggested visit durations alone add up to {visit_total} min, against a {window}-min window (walking not included).")
    for p in required:
        step = try_stop(p, None, None, ctx)
        if step.code in ("unknown_hours", "closed", "window_exceeded"):
            notes.append(step.reason + ".")
        if reason := constraint_violation(p, c):
            notes.append(f"{p.name}: {reason}.")
    for p in required:
        others = [q for q in required if q.id != p.id]
        legs = [walk_leg(p.location, q.location, speed_kmh=ctx.speed_kmh) for q in others]
        if others and not any(l.walkable for l in legs):
            nearest = min(legs, key=lambda l: l.route_km)
            notes.append(f"{p.name} is at least ~{nearest.route_km:.1f} km on foot from every other requested place, beyond the {config.MAX_WALK_LEG_KM:g} km walking limit.")

    for k in range(len(required), 0, -1):
        best = None
        for combo in combinations(required, k):
            for order in permutations(combo):
                stops, failed = schedule(list(order), ctx)
                if not failed:
                    v = value(stops, scores)
                    if best is None or v > best[0]:
                        best = (v, stops)
        if best:
            itin = _itinerary(c, best[1])
            kept = set(itin.poi_ids())
            dropped = [p for p in required if p.id not in kept]
            status = "ok" if not dropped else "partial"
            if dropped:
                notes.append(f"Not possible as requested. The most that fits is {k} of {len(required)}; left out: {', '.join(p.name for p in dropped)}.")
            return PlanResult(status=status, itinerary=itin, validation=validate(itin, c, repo, rain, exempt=kept), notes=notes)
    notes.append("None of the requested places can be visited within this window.")
    return PlanResult(status="infeasible", notes=notes)


def _retime_changes(old: Itinerary, new: Itinerary) -> list[str]:
    before = {s.poi_id: s for s in old.stops}
    out = []
    for s in new.stops:
        if (prev := before.get(s.poi_id)) and prev.start != s.start:
            out.append(f"{s.name} moved from {prev.start:%H:%M} to {s.start:%H:%M}.")
    return out
