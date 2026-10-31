from datetime import UTC, datetime

import pytest

from app.hours import HoursStatus, check_visit, earliest_visit_start, local_dt, parse_range
from tests.conftest import MON, TUE, WED


def test_parse_range_variants():
    assert parse_range("09:00-17:00") == (540, 1020)
    assert parse_range("00:00-24:00") == (0, 1440)
    assert parse_range("20:00-02:00") == (1200, 1560)  # closes next day


def test_open_within_hours(poi):
    assert check_visit(poi("CF-001"), local_dt(WED, "13:00"), 85).ok


def test_visit_must_finish_before_closing(poi):
    # New Fortress closes 15:30; a 65-min visit from 14:45 would end 15:50
    result = check_visit(poi("CF-002"), local_dt(WED, "14:45"), 65)
    assert result.status is HoursStatus.CLOSED
    assert check_visit(poi("CF-002"), local_dt(WED, "14:25"), 65).ok


def test_null_weekday_means_closed(poi):
    # New Fortress: tue is null in the fixture
    result = check_visit(poi("CF-002"), local_dt(TUE, "11:00"), 65)
    assert result.status is HoursStatus.CLOSED
    assert "Tuesday" in result.reason


def test_null_hours_means_unknown_not_open(poi):
    result = check_visit(poi("CF-004"), local_dt(TUE, "12:00"), 25)  # Saint Spyridon
    assert result.status is HoursStatus.UNKNOWN
    assert not result.ok


@pytest.mark.parametrize("poi_id", ["CF-014", "CF-017", "CF-031"])
def test_scenario_a_monday_closures(poi, poi_id):
    # Art Gallery, Asian Art and Archaeological museums are closed on Mondays
    assert check_visit(poi(poi_id), local_dt(MON, "13:00"), 30).status is HoursStatus.CLOSED


def test_continuous_access_place(poi):
    spianada = poi("CF-033")  # 00:00-24:00
    assert check_visit(spianada, local_dt(TUE, "11:00"), 30).ok
    assert check_visit(spianada, local_dt(TUE, "23:30"), 30).ok  # ends exactly at 24:00


def test_overnight_hours_spill_into_next_day(poi):
    bar = poi("CF-048")  # 20:00-02:00 every day
    assert check_visit(bar, local_dt(TUE, "21:00"), 60).ok
    assert check_visit(bar, local_dt(WED, "00:30"), 60).ok  # opened on Tuesday
    assert not check_visit(bar, local_dt(WED, "01:30"), 60).ok  # runs past 02:00
    assert not check_visit(bar, local_dt(WED, "15:00"), 60).ok


def test_naive_datetime_is_interpreted_as_athens_local(poi):
    naive = datetime(2026, 10, 7, 13, 0)
    assert check_visit(poi("CF-001"), naive, 85).ok


def test_utc_input_is_converted(poi):
    # 10:30 UTC = 13:30 Athens (EEST, UTC+3): the visit fits.
    # 13:30 UTC = 16:30 Athens: an 85-min Old Fortress visit runs past 17:00.
    assert check_visit(poi("CF-001"), datetime(2026, 10, 7, 10, 30, tzinfo=UTC), 85).ok
    assert not check_visit(poi("CF-001"), datetime(2026, 10, 7, 13, 30, tzinfo=UTC), 85).ok


def test_earliest_start_waits_for_opening(poi):
    # Casa Parlante opens at 10:00
    start = earliest_visit_start(poi("CF-007"), local_dt(TUE, "09:40"), 55)
    assert start == local_dt(TUE, "10:00")
    assert earliest_visit_start(poi("CF-007"), local_dt(TUE, "16:30"), 55) is None
    assert earliest_visit_start(poi("CF-004"), local_dt(TUE, "10:00"), 25) is None  # unknown hours
