"""Independent validation of an itinerary against hard constraints.

Recomputes everything from the dataset instead of trusting the times stored in
the itinerary, so it also catches plans edited by hand or by a faulty revision.
"""

from datetime import timedelta

from app import config
from app.data import POIRepository
from app.geo import walk_leg
from app.hours import check_visit, local_dt
from app.itinerary import Issue, Itinerary, PlanConstraints, ValidationReport
from app.recommend import constraint_violation
from app.weather import RainWindows, overlaps


def validate(
    itin: Itinerary,
    c: PlanConstraints,
    repo: POIRepository,
    rain: RainWindows | None = None,
    exempt: set[str] = frozenset(),
) -> ValidationReport:
    """`exempt` lists stops the visitor explicitly asked to keep: their constraint
    violations (e.g. not confirmed family-suitable) become warnings, not errors."""
    r = ValidationReport()
    window_start, window_end = local_dt(c.date, c.start), local_dt(c.date, c.end)
    seen: set[str] = set()

    for i, s in enumerate(itin.stops):
        poi = repo.get(s.poi_id)
        if poi is None:
            r.errors.append(Issue(code="unknown_poi", message=f"Stop {i + 1} '{s.name}' is not in the dataset.", poi_id=s.poi_id))
            continue
        if s.poi_id in seen:
            r.errors.append(Issue(code="duplicate", message=f"{poi.name} appears more than once.", poi_id=poi.id))
        seen.add(s.poi_id)

        if s.start < window_start or s.end > window_end:
            r.errors.append(Issue(code="window_exceeded", message=f"{poi.name} ({s.start:%H:%M}-{s.end:%H:%M}) is outside {c.start:%H:%M}-{c.end:%H:%M}.", poi_id=poi.id))
        if s.visit_minutes < poi.suggested_visit_minutes:
            r.errors.append(Issue(code="visit_too_short", message=f"{poi.name} has {s.visit_minutes} min, less than its {poi.suggested_visit_minutes}-min suggested visit.", poi_id=poi.id))
        if s.start < s.arrive:
            r.errors.append(Issue(code="time_order", message=f"{poi.name} starts before arrival.", poi_id=poi.id))

        hours = check_visit(poi, s.start, s.visit_minutes)
        if not hours.ok:
            r.errors.append(Issue(code=f"hours_{hours.status.value}", message=hours.reason + ".", poi_id=poi.id))

        if violation := constraint_violation(poi, c):
            target = r.warnings if poi.id in exempt else r.errors
            target.append(Issue(code="constraint", message=f"{poi.name}: {violation}.", poi_id=poi.id))

        if rain and not poi.is_sheltered and overlaps(rain, s.start, s.end):
            r.errors.append(Issue(code="rain_exposed", message=f"{poi.name} is {poi.setting.value} and scheduled during forecast rain.", poi_id=poi.id))

        if i > 0:
            prev = itin.stops[i - 1]
            prev_poi = repo.get(prev.poi_id)
            if prev_poi is None:
                continue
            leg = walk_leg(prev_poi.location, poi.location, speed_kmh=itin.walk_speed_kmh)
            if not leg.walkable:
                r.errors.append(Issue(code="not_walkable", message=f"{prev_poi.name} -> {poi.name} is ~{leg.route_km:.1f} km on foot, beyond the {config.MAX_WALK_LEG_KM:g} km limit.", poi_id=poi.id))
            earliest_arrival = prev.end + timedelta(minutes=leg.minutes)
            if s.arrive < earliest_arrival:
                r.errors.append(Issue(code="walk_too_fast", message=f"Arrival at {poi.name} ({s.arrive:%H:%M}) is earlier than the estimated walk allows ({earliest_arrival:%H:%M}).", poi_id=poi.id))
            if rain and overlaps(rain, prev.end, s.arrive):
                r.warnings.append(Issue(code="walk_in_rain", message=f"The walk to {poi.name} ({prev.end:%H:%M}-{s.arrive:%H:%M}) falls in forecast rain.", poi_id=poi.id))

    if not itin.stops:
        r.errors.append(Issue(code="empty", message="The itinerary has no stops."))

    r.checks = [
        "all stops exist in the dataset and appear once",
        f"every visit lies within {c.start:%H:%M}-{c.end:%H:%M} on {c.date:%A %d %B %Y} (Europe/Athens)",
        "every visit fits the fixture opening hours and its full suggested duration",
        f"walking legs recomputed at {itin.walk_speed_kmh:g} km/h and within {config.MAX_WALK_LEG_KM:g} km",
        "visitor constraints (exclusions, setting, family suitability) respected",
    ] + (["no outdoor or mixed stop during forecast rain"] if rain else [])
    return r
