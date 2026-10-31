"""Plain-text rendering of itineraries. The timetable shown to the visitor comes
from here, i.e. from planner output, never from LLM-generated text."""

from app.itinerary import Itinerary, ValidationReport


def itinerary_table(itin: Itinerary) -> str:
    lines = [f"Itinerary for {itin.date:%A %d %B %Y}, {itin.window_start:%H:%M}-{itin.window_end:%H:%M} (Europe/Athens)"]
    for i, s in enumerate(itin.stops, 1):
        walk = ""
        if i > 1:
            walk = f"  <- walk ~{s.walk_minutes} min (~{s.walk_km:.1f} km) + {s.buffer_minutes} min buffer"
        wait = f", waits {s.wait_minutes} min for opening" if s.wait_minutes else ""
        lines.append(f"  {i}. {s.start:%H:%M}-{s.end:%H:%M}  {s.name} [{s.poi_id}] · {s.setting.value}{wait}{walk}")
    if itin.stops:
        lines.append(
            f"  Finish {itin.finish:%H:%M}, {itin.free_minutes} min spare. "
            f"Walking ~{itin.total_walk_minutes} min / ~{itin.total_walk_km:.1f} km in total at {itin.walk_speed_kmh:g} km/h."
        )
    lines.append(
        "  Walking times are straight-line estimates x1.3; opening hours and durations are exercise fixtures, not live visitor information."
    )
    return "\n".join(lines)


def validation_summary(report: ValidationReport) -> str:
    if report.ok and not report.warnings:
        return "Checks passed: " + "; ".join(report.checks) + "."
    parts = [f"ERROR: {e.message}" for e in report.errors] + [f"Note: {w.message}" for w in report.warnings]
    return "\n".join(parts)
