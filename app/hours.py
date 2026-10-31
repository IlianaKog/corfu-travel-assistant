"""Opening-hours interpretation in Europe/Athens local time.

Fixture rules (from the dataset's fields_note):
- whole field null    -> unknown; never treated as open
- weekday null        -> closed that day
- end earlier than start (20:00-02:00) -> closes on the next calendar day
- 00:00-24:00         -> continuous access (exercise only)

Arithmetic is done in local wall-clock time. That is exact except across a DST
change at night, which no daytime itinerary crosses.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from app.config import TZ
from app.models import POI, WEEKDAYS


class HoursStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VisitCheck:
    status: HoursStatus
    reason: str

    @property
    def ok(self) -> bool:
        return self.status is HoursStatus.OPEN


def _parse_hhmm(value: str) -> int:
    hh, mm = value.strip().split(":")
    minutes = int(hh) * 60 + int(mm)
    if not 0 <= minutes <= 24 * 60:
        raise ValueError(f"invalid time {value!r}")
    return minutes


def parse_range(value: str) -> tuple[int, int]:
    """Return (start, end) minutes from local midnight; end may exceed 1440."""
    start_s, end_s = value.split("-")
    start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    if end <= start:
        end += 24 * 60
    return start, end


def local_dt(day: date, hhmm: str | time) -> datetime:
    t = time.fromisoformat(hhmm) if isinstance(hhmm, str) else hhmm
    return datetime.combine(day, t, tzinfo=TZ)


def to_local(dt: datetime) -> datetime:
    """Interpret naive datetimes as Athens local time; convert aware ones."""
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def open_intervals(poi: POI, day: date) -> list[tuple[datetime, datetime]] | None:
    """Opening intervals that touch `day`, including an overnight spill from the
    previous day. Returns None when the POI's hours are unknown."""
    if poi.opening_hours_fixture is None:
        return None
    intervals = []
    for offset in (-1, 0):
        d = day + timedelta(days=offset)
        spec = poi.opening_hours_fixture.get(WEEKDAYS[d.weekday()])
        if spec is None:
            continue
        start, end = parse_range(spec)
        midnight = datetime.combine(d, time(0), tzinfo=TZ)
        interval = (midnight + timedelta(minutes=start), midnight + timedelta(minutes=end))
        if interval[1] > datetime.combine(day, time(0), tzinfo=TZ):
            intervals.append(interval)
    return intervals


def check_visit(poi: POI, start: datetime, minutes: int) -> VisitCheck:
    """Is the POI open for the whole visit [start, start + minutes]?"""
    start = to_local(start)
    end = start + timedelta(minutes=minutes)
    intervals = open_intervals(poi, start.date())
    if intervals is None:
        return VisitCheck(HoursStatus.UNKNOWN, f"{poi.name}: no confirmed opening hours")
    for open_at, close_at in intervals:
        if open_at <= start and end <= close_at:
            return VisitCheck(HoursStatus.OPEN, f"{poi.name}: open {_fmt(open_at)}-{_fmt(close_at)}")
    if not intervals:
        return VisitCheck(HoursStatus.CLOSED, f"{poi.name}: closed on {start:%A}")
    windows = ", ".join(f"{_fmt(a)}-{_fmt(b)}" for a, b in intervals)
    return VisitCheck(
        HoursStatus.CLOSED,
        f"{poi.name}: a {minutes}-min visit from {start:%H:%M} does not fit opening hours {windows}",
    )


def earliest_visit_start(poi: POI, arrival: datetime, minutes: int) -> datetime | None:
    """Earliest start >= arrival on the same day at which the full visit fits,
    waiting for opening if necessary. None if impossible or hours unknown."""
    arrival = to_local(arrival)
    intervals = open_intervals(poi, arrival.date())
    if not intervals:
        return None
    for open_at, close_at in sorted(intervals):
        start = max(arrival, open_at)
        if start + timedelta(minutes=minutes) <= close_at:
            return start
    return None


def _fmt(dt: datetime) -> str:
    return "24:00" if dt.time() == time(0) else dt.strftime("%H:%M")
