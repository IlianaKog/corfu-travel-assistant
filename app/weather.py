"""Weather providers and rain-window derivation.

Two providers share one interface:
- OpenMeteoProvider: live forecast (keyless). Used for every real request.
- FixtureProvider:   a recorded/synthetic response, injected explicitly for
                     reproducible replanning tests. Never used implicitly.

Time convention: an HourlyPoint at HH:00 covers [HH:00, HH+1:00). Open-Meteo
reports precipitation and weather code as the *preceding* hour, so live data is
re-aligned; the synthetic fixture already follows this convention ("rain
begins after 15:00" with 3.2 mm stamped 15:00).
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from app import config
from app.hours import to_local

log = logging.getLogger(__name__)

# WMO weather interpretation codes that indicate precipitation
# (drizzle, rain, freezing rain, snow, showers, thunderstorm).
RAIN_WMO_CODES = set(range(51, 68)) | set(range(71, 78)) | set(range(80, 87)) | {95, 96, 99}

WMO_DESCRIPTIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle", 56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain", 66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers", 85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

RainWindows = list[tuple[datetime, datetime]]

CORFU_TOWN = (39.6243, 19.9217)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherUnavailable(Exception):
    """No trustworthy data for the request. The message is safe to show the user."""


class HourlyPoint(BaseModel):
    local_time: datetime  # Europe/Athens; covers [local_time, local_time + 1h)
    temperature_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: float | None = None
    weather_code: int | None = None

    @property
    def is_rainy(self) -> bool:
        if self.precipitation_mm is not None and self.precipitation_mm >= config.RAIN_THRESHOLD_MM:
            return True
        return self.weather_code in RAIN_WMO_CODES

    @property
    def description(self) -> str:
        return WMO_DESCRIPTIONS.get(self.weather_code, "unknown") if self.weather_code is not None else "unknown"


class WeatherReport(BaseModel):
    source: str  # "open-meteo" | "synthetic_fixture"
    is_live: bool
    place: str
    hourly: list[HourlyPoint]
    note: str = ""
    request_url: str | None = None

    def between(self, start: datetime, end: datetime) -> "WeatherReport":
        start, end = to_local(start), to_local(end)
        # keep hours [t, t+1h) that overlap [start, end)
        points = [h for h in self.hourly if to_local(h.local_time) < end and to_local(h.local_time) + timedelta(hours=1) > start]
        return self.model_copy(update={"hourly": points})

    def rain_windows(self) -> RainWindows:
        windows: RainWindows = []
        for h in sorted(self.hourly, key=lambda p: p.local_time):
            if not h.is_rainy:
                continue
            start = to_local(h.local_time)
            end = start + timedelta(hours=1)
            if windows and windows[-1][1] == start:
                windows[-1] = (windows[-1][0], end)
            else:
                windows.append((start, end))
        return windows

    def missing_hours(self, start: datetime, end: datetime) -> list[datetime]:
        """Whole hours in [start, end) with no data point."""
        have = {to_local(h.local_time).replace(minute=0, second=0, microsecond=0) for h in self.hourly}
        out, t = [], to_local(start).replace(minute=0, second=0, microsecond=0)
        while t < to_local(end):
            if t not in have:
                out.append(t)
            t += timedelta(hours=1)
        return out


class WeatherProvider(Protocol):
    def forecast(self, start: datetime, end: datetime) -> WeatherReport: ...


class OpenMeteoProvider:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 10.0, place: str = "Corfu Town"):
        self._client = client or httpx.Client(timeout=timeout)
        self.place = place

    def forecast(self, start: datetime, end: datetime) -> WeatherReport:
        start, end = to_local(start), to_local(end)
        params = {
            "latitude": CORFU_TOWN[0],
            "longitude": CORFU_TOWN[1],
            "hourly": "temperature_2m,precipitation,precipitation_probability,weather_code",
            "timezone": config.TIMEZONE_NAME,
            "start_date": start.date().isoformat(),
            # +1 day: the hour after `end` carries precipitation for the last hour
            "end_date": (end.date() + timedelta(days=1)).isoformat(),
        }
        try:
            resp = self._client.get(OPEN_METEO_URL, params=params)
        except httpx.HTTPError as e:
            raise WeatherUnavailable(f"The weather service could not be reached ({type(e).__name__}).") from e
        if resp.status_code == 400:
            reason = _safe_json(resp).get("reason", "request rejected")
            raise WeatherUnavailable(f"Open-Meteo has no forecast for this date: {reason}.")
        if resp.status_code != 200:
            raise WeatherUnavailable(f"The weather service returned HTTP {resp.status_code}.")
        report = parse_open_meteo(_safe_json(resp), place=self.place)
        report.request_url = str(resp.request.url)
        return report.between(start, end)


def parse_open_meteo(payload: dict, place: str = "Corfu Town") -> WeatherReport:
    """Align Open-Meteo hourly arrays to the [HH, HH+1) convention: temperature is
    instantaneous at HH; precipitation, probability and code stamped HH+1 describe
    the hour HH..HH+1."""
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        raise WeatherUnavailable("The weather service returned no hourly data.")
    temp = h.get("temperature_2m", [None] * len(times))
    precip = h.get("precipitation", [None] * len(times))
    prob = h.get("precipitation_probability", [None] * len(times))
    code = h.get("weather_code", [None] * len(times))
    points = []
    for i in range(len(times) - 1):
        points.append(
            HourlyPoint(
                local_time=to_local(datetime.fromisoformat(times[i])),
                temperature_c=temp[i],
                precipitation_mm=precip[i + 1],
                precipitation_probability=prob[i + 1],
                weather_code=code[i + 1],
            )
        )
    return WeatherReport(source="open-meteo", is_live=True, place=place, hourly=points)


class FixtureProvider:
    """Serves a recorded weather response for the date it covers, and refuses
    other dates rather than mixing synthetic and live data."""

    def __init__(self, path: Path = config.RAIN_FIXTURE_PATH):
        self.path = Path(path)
        self.report = load_fixture(self.path)
        self.dates = {to_local(h.local_time).date() for h in self.report.hourly}

    def forecast(self, start: datetime, end: datetime) -> WeatherReport:
        if to_local(start).date() not in self.dates:
            covered = ", ".join(sorted(d.isoformat() for d in self.dates))
            raise WeatherUnavailable(f"The injected weather fixture only covers {covered}; no data for {to_local(start):%Y-%m-%d}.")
        return self.report.between(start, end)


def load_fixture(path: Path = config.RAIN_FIXTURE_PATH) -> WeatherReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return WeatherReport(
        source="synthetic_fixture",
        is_live=bool(raw.get("is_live", False)),
        place=raw.get("place", "Corfu"),
        hourly=[HourlyPoint.model_validate(h) for h in raw["hourly"]],
        note=raw.get("scenario", ""),
    )


def summarize(report: WeatherReport, start: datetime, end: datetime) -> dict:
    """Compact, factual view for the LLM: no interpretation beyond thresholds."""
    rain = report.rain_windows()
    missing = report.missing_hours(start, end)
    probabilities = [h.precipitation_probability for h in report.hourly if h.precipitation_probability is not None]
    return {
        "source": report.source,
        "is_live": report.is_live,
        "place": report.place,
        "period": f"{to_local(start):%Y-%m-%d %H:%M} to {to_local(end):%Y-%m-%d %H:%M} Europe/Athens",
        "rain_expected": bool(rain),
        "rain_windows": [f"{a:%Y-%m-%d %H:%M}-{b:%H:%M}" for a, b in rain],
        "rain_rule": f"an hour counts as rainy at >= {config.RAIN_THRESHOLD_MM} mm or a WMO precipitation code",
        # An explicit sentence, not null: in the demo the model turned a missing value
        # into "a high probability of moderate rain" for the synthetic fixture.
        "max_precipitation_probability": max(probabilities) if probabilities
        else "not provided by this source; do not state or estimate a probability",
        "hours": [
            {
                "time": f"{to_local(h.local_time):%Y-%m-%d %H:%M}",
                "temperature_c": h.temperature_c,
                "precipitation_mm": h.precipitation_mm,
                "precipitation_probability": h.precipitation_probability,
                "conditions": h.description,
            }
            for h in report.hourly
        ],
        "hours_without_data": [f"{t:%H:%M}" for t in missing],
        "note": report.note,
    }


def overlaps(windows: RainWindows, start: datetime, end: datetime) -> bool:
    return any(a < end and start < b for a, b in windows)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def provider_for(fixture_path: str | Path | None) -> WeatherProvider:
    return FixtureProvider(Path(fixture_path)) if fixture_path else OpenMeteoProvider()


def now_local() -> datetime:
    return datetime.now(config.TZ)
