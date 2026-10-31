"""Weather providers. Offline by default: Open-Meteo is replayed from a recorded
response through httpx.MockTransport. Set RUN_LIVE=1 to also hit the real API."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.config import TZ
from app.weather import (
    FixtureProvider, OpenMeteoProvider, WeatherUnavailable, load_fixture, now_local, parse_open_meteo, summarize,
)

RECORDED = Path(__file__).parent / "fixtures" / "open_meteo_2026-10-29.json"


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def replay(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=json.loads(RECORDED.read_text()))


def at(day: str, hh: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hh:02d}:00").replace(tzinfo=TZ)


# --- live provider, replayed ---------------------------------------------------

def test_open_meteo_request_and_parse():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return replay(request)

    report = OpenMeteoProvider(mock_client(handler)).forecast(at("2026-10-29", 12), at("2026-10-29", 18))
    assert seen["timezone"] == "Europe/Athens" and seen["start_date"] == "2026-10-29"
    assert report.is_live and report.source == "open-meteo"
    assert [h.local_time.hour for h in report.hourly] == list(range(12, 18))
    assert all(h.local_time.tzinfo is not None for h in report.hourly)


def test_precipitation_is_realigned_to_the_hour_it_falls_in():
    payload = {"hourly": {
        "time": ["2026-10-29T14:00", "2026-10-29T15:00", "2026-10-29T16:00"],
        "temperature_2m": [20, 19, 18],
        "precipitation": [0.0, 0.0, 2.0],  # 2.0 mm fell during 15:00-16:00
        "precipitation_probability": [0, 10, 80],
        "weather_code": [1, 2, 63],
    }}
    report = parse_open_meteo(payload)
    by_hour = {h.local_time.hour: h for h in report.hourly}
    assert by_hour[15].precipitation_mm == 2.0 and by_hour[15].is_rainy
    assert not by_hour[14].is_rainy
    assert [(a.hour, b.hour) for a, b in report.rain_windows()] == [(15, 16)]


def test_out_of_range_date_is_reported_not_guessed():
    def handler(request):
        return httpx.Response(400, json={"error": True, "reason": "Parameter 'start_date' is out of allowed range"})

    with pytest.raises(WeatherUnavailable, match="out of allowed range"):
        OpenMeteoProvider(mock_client(handler)).forecast(at("2026-12-20", 12), at("2026-12-20", 18))


def test_network_failure_is_reported_not_guessed():
    def handler(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(WeatherUnavailable, match="could not be reached"):
        OpenMeteoProvider(mock_client(handler)).forecast(at("2026-10-29", 12), at("2026-10-29", 18))


def test_server_error_is_reported():
    with pytest.raises(WeatherUnavailable, match="HTTP 503"):
        OpenMeteoProvider(mock_client(lambda r: httpx.Response(503))).forecast(at("2026-10-29", 12), at("2026-10-29", 18))


# --- injected fixture ------------------------------------------------------------

def test_fixture_is_labelled_synthetic():
    report = FixtureProvider().forecast(at("2026-10-07", 13), at("2026-10-07", 17))
    assert report.source == "synthetic_fixture" and not report.is_live
    s = summarize(report, at("2026-10-07", 13), at("2026-10-07", 17))
    assert s["rain_expected"] and s["rain_windows"] == ["2026-10-07 15:00-17:00"]
    assert s["hours_without_data"] == []


def test_missing_probability_is_stated_not_left_null():
    """Seen in the demo: with null probabilities the model claimed "a high probability"."""
    s = summarize(load_fixture(), at("2026-10-07", 13), at("2026-10-07", 17))
    assert "not provided" in s["max_precipitation_probability"]
    live = OpenMeteoProvider(mock_client(replay)).forecast(at("2026-10-29", 12), at("2026-10-30", 18))
    assert isinstance(summarize(live, at("2026-10-29", 12), at("2026-10-30", 18))["max_precipitation_probability"], float)


def test_fixture_refuses_other_dates():
    with pytest.raises(WeatherUnavailable, match="only covers 2026-10-07"):
        FixtureProvider().forecast(at("2026-10-08", 13), at("2026-10-08", 17))


def test_fixture_reports_hours_without_data():
    report = load_fixture()
    s = summarize(report.between(at("2026-10-07", 11), at("2026-10-07", 15)), at("2026-10-07", 11), at("2026-10-07", 15))
    assert s["hours_without_data"] == ["11:00", "12:00"]


# --- real network, opt-in --------------------------------------------------------

@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call Open-Meteo")
def test_live_next_24_hours():
    start = now_local()
    report = OpenMeteoProvider().forecast(start, start + timedelta(hours=24))
    assert report.is_live and len(report.hourly) >= 23
    assert report.request_url and "open-meteo.com" in report.request_url
