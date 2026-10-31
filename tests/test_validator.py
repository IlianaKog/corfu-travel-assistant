"""The validator must catch broken itineraries on its own, not trust the planner."""

from datetime import time, timedelta

import pytest

from app import planner
from app.itinerary import PlanConstraints, Stop
from app.models import Setting
from app.recommend import keyword_scores
from app.validator import validate
from app.weather import load_fixture
from tests.conftest import TUE, WED


@pytest.fixture
def good(repo):
    c = PlanConstraints(date=TUE, start=time(11), end=time(15), interests=["history"])
    res = planner.plan(c, repo, keyword_scores(repo.all(), c.interests))
    assert res.validation.ok
    return c, res.itinerary


def codes(report):
    return {e.code for e in report.errors}


def test_invented_attraction(repo, good):
    c, itin = good
    s = itin.stops[0]
    itin.stops.append(Stop(poi_id="CF-777", name="Corfu Aquarium", setting=Setting.INDOOR, arrive=s.arrive, start=s.start, end=s.end))
    assert "unknown_poi" in codes(validate(itin, c, repo))


def test_overrun_window(repo, good):
    c, itin = good
    last = itin.stops[-1]
    last.start += timedelta(hours=2)
    last.end += timedelta(hours=2)
    last.arrive += timedelta(hours=2)
    assert "window_exceeded" in codes(validate(itin, c, repo))


def test_shortened_visit(repo, good):
    c, itin = good
    itin.stops[0].end = itin.stops[0].start + timedelta(minutes=5)
    assert "visit_too_short" in codes(validate(itin, c, repo))


def test_impossible_walk(repo, good):
    c, itin = good
    second = itin.stops[1]
    second.arrive = itin.stops[0].end  # teleport
    assert "walk_too_fast" in codes(validate(itin, c, repo))


def test_closed_day(repo, good):
    c, itin = good
    s = itin.stops[0]
    poi = repo.get("CF-002")  # New Fortress, closed on Tuesday
    itin.stops[0] = Stop(poi_id=poi.id, name=poi.name, setting=poi.setting, arrive=s.start, start=s.start, end=s.start + timedelta(minutes=poi.suggested_visit_minutes))
    assert "hours_closed" in codes(validate(itin, c, repo))


def test_child_constraint(repo, good):
    c, itin = good
    child = c.model_copy(update={"with_child": True})
    report = validate(itin, child, repo)
    non_family = [s for s in itin.stops if repo.get(s.poi_id).family_suitability_fixture is not True]
    if non_family:
        assert "constraint" in codes(report)
        # stops the visitor asked to keep are downgraded to warnings
        exempt = validate(itin, child, repo, exempt={s.poi_id for s in non_family})
        assert "constraint" not in codes(exempt)
        assert exempt.warnings


def test_rain_exposure(repo):
    c = PlanConstraints(date=WED, start=time(13), end=time(17), interests=["history"], settings=[Setting.OUTDOOR])
    itin = planner.plan(c, repo, keyword_scores(repo.all(), c.interests)).itinerary
    assert "rain_exposed" in codes(validate(itin, c, repo, load_fixture().rain_windows()))
