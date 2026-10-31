"""Planner behaviour on the brief's scenarios and on general invariants.

Assertions check properties (closures respected, window kept, rain avoided),
not exact itineraries, so tuning the objective does not break them.
"""

import itertools
from datetime import time

import pytest

from app import planner
from app.hours import open_intervals
from app.itinerary import PlanConstraints
from app.models import Category, Setting
from app.recommend import keyword_scores
from app.weather import load_fixture, overlaps
from tests.conftest import MON, TUE, WED


def run(repo, **kw):
    c = PlanConstraints(**kw)
    return c, planner.plan(c, repo, keyword_scores(repo.all(), c.interests))


def assert_sound(result, c):
    assert result.itinerary is not None
    assert result.validation.ok, [e.message for e in result.validation.errors]
    stops = result.itinerary.stops
    assert stops[0].start.time() >= c.start
    assert stops[-1].end.time() <= c.end


# --- scenario A: art & museums on a Monday -----------------------------------

def test_art_museums_monday_skips_monday_closures(repo):
    c, res = run(repo, date=MON, start=time(13), end=time(17), interests=["art", "museums"])
    assert_sound(res, c)
    ids = set(res.itinerary.poi_ids())
    assert not ids & {"CF-014", "CF-017", "CF-031"}  # closed on Mondays
    assert any("closed on Monday" in n for n in res.notes)
    for s in res.itinerary.stops:
        assert {"museum", "art", "gallery"} & set(repo.get(s.poi_id).tags)


# --- scenario B: historical walk + replace stop 2 for a child -----------------

@pytest.fixture
def historical_tuesday(repo):
    return run(repo, date=TUE, start=time(11), end=time(15), interests=["historical attractions"])


def test_historical_tuesday_respects_closures(historical_tuesday):
    c, res = historical_tuesday
    assert_sound(res, c)
    assert not set(res.itinerary.poi_ids()) & {"CF-002", "CF-029", "CF-012"}  # closed Tuesdays
    assert len(res.itinerary.stops) >= 3


def test_replace_second_stop_for_child_keeps_the_rest(repo, historical_tuesday):
    c, res = historical_tuesday
    before = res.itinerary.poi_ids()
    child = c.model_copy(update={"with_child": True, "child_age": 10})
    out = planner.replace_stop(res.itinerary, 1, child, repo, keyword_scores(repo.all(), child.interests))
    assert out.status == "ok"
    after = out.itinerary.poi_ids()
    assert after[1] != before[1]
    assert repo.get(after[1]).family_suitability_fixture is True
    assert after[:1] + after[2:] == before[:1] + before[2:]  # others kept, same order
    assert out.itinerary.finish.time() <= time(15)
    assert out.validation.ok
    assert out.itinerary.walk_speed_kmh < res.itinerary.walk_speed_kmh  # slower with a child


def test_replace_reports_when_nothing_fits(repo, historical_tuesday):
    c, res = historical_tuesday
    impossible = c.model_copy(update={"categories": [Category.NIGHTLIFE]})  # bars open 20:00
    out = planner.replace_stop(res.itinerary, 1, impossible, repo, keyword_scores(repo.all(), []))
    assert out.status == "infeasible"
    assert out.itinerary.poi_ids() == res.itinerary.poi_ids()  # unchanged


def test_failed_child_replacement_is_explained_with_numbers(repo):
    """Seen live: a tight plan (1 min spare) where nothing fits at child pace.
    The answer must give the arithmetic and note the current stop already qualifies."""
    c = PlanConstraints(date=TUE, start=time(11), end=time(15), categories=[Category.HISTORY])
    ids = ["CF-028", "CF-027", "CF-001", "CF-010", "CF-003", "CF-009"]
    ctx = planner.Context.from_constraints(c)
    stops, failed = planner.schedule([repo.get(i) for i in ids], ctx)
    assert failed is None
    itin = planner._itinerary(c, stops)
    child = c.model_copy(update={"with_child": True, "child_age": 10})
    out = planner.replace_stop(itin, 1, child, repo, keyword_scores(repo.all(), ["history"]))
    assert out.status == "infeasible" and out.itinerary.poi_ids() == ids
    text = " ".join(out.notes)
    assert "Gate of the Old Fortress [CF-027] itself already meets" in text
    assert "leaving 15 min" in text and "3.5 km/h" in text
    assert "Options:" in text


def test_replace_rejects_missing_stop_index(repo, historical_tuesday):
    c, res = historical_tuesday
    out = planner.replace_stop(res.itinerary, 9, c, repo, {})
    assert out.status == "infeasible"


# --- scenario C: five named places in 90 minutes -------------------------------

def test_five_places_in_ninety_minutes_is_refused_with_reasons(repo):
    names = ["Old Fortress", "New Fortress", "Achilleion Palace", "Mon Repos Museum of Palaiopolis", "Museum of Asian Art"]
    ids = [repo.resolve_name(n).id for n in names]
    c, res = run(repo, date=TUE, start=time(11), end=time(12, 30), required_poi_ids=ids)
    assert res.status == "partial"
    assert_sound(res, c)
    text = " ".join(res.notes)
    assert "355 min" in text and "90-min" in text
    assert "New Fortress: closed on Tuesday" in text
    assert "Achilleion Palace" in text and "walking limit" in text
    assert not {"CF-002", "CF-021"} & set(res.itinerary.poi_ids())


def test_feasible_named_places_are_all_kept(repo):
    ids = ["CF-009", "CF-016", "CF-032"]  # three short Old Town stops
    c, res = run(repo, date=TUE, start=time(11), end=time(13), required_poi_ids=ids)
    assert res.status == "ok"
    assert set(res.itinerary.poi_ids()) == set(ids)


def test_unknown_required_id_is_refused(repo):
    _, res = run(repo, date=TUE, start=time(11), end=time(13), required_poi_ids=["CF-999"])
    assert res.status == "infeasible"


# --- scenario D: outdoor plan, then rain from 15:00 ----------------------------

def test_rain_fixture_rain_windows():
    rain = load_fixture().rain_windows()
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in rain] == [("15:00", "18:00")]


def test_weather_adaptation_moves_exposure_out_of_rain(repo):
    c, res = run(repo, date=WED, start=time(13), end=time(17), interests=["historical attractions"], settings=[Setting.OUTDOOR])
    assert_sound(res, c)
    rain = load_fixture().rain_windows()
    assert any(overlaps(rain, s.start, s.end) for s in res.itinerary.stops)  # the original plan is rained on

    out = planner.adapt_to_weather(res.itinerary, c, repo, keyword_scores(repo.all(), c.interests), rain)
    assert out.status == "ok"
    assert out.validation.ok, [e.message for e in out.validation.errors]
    for s in out.itinerary.stops:
        assert s.setting is Setting.INDOOR or not overlaps(rain, s.start, s.end)
        assert repo.get(s.poi_id).hours_known  # shelter only with confirmed hours
    assert out.itinerary.window_start == res.itinerary.window_start
    assert out.itinerary.finish.time() <= time(17)
    assert out.changes


def test_weather_adaptation_without_rain_is_unchanged(repo, historical_tuesday):
    c, res = historical_tuesday
    out = planner.adapt_to_weather(res.itinerary, c, repo, {}, rain=[])
    assert out.status == "unchanged"
    assert out.itinerary.poi_ids() == res.itinerary.poi_ids()


# --- constraints ---------------------------------------------------------------

def test_avoid_museums_with_child(repo):
    c, res = run(repo, date=TUE, start=time(11), end=time(15), interests=["history"], exclude_tags=["museums"], with_child=True)
    assert_sound(res, c)
    for s in res.itinerary.stops:
        poi = repo.get(s.poi_id)
        assert "museum" not in poi.tags and "gallery" not in poi.tags
        assert poi.family_suitability_fixture is True


def test_unknown_hours_are_never_scheduled(repo):
    _, res = run(repo, date=TUE, start=time(10), end=time(14), interests=["religion", "jewish"])
    for s in res.itinerary.stops:
        assert repo.get(s.poi_id).hours_known


def test_new_plans_keep_a_reserve_that_revisions_may_use(repo):
    c, res = run(repo, date=TUE, start=time(11), end=time(15), interests=["history"])
    assert res.itinerary.free_minutes >= 10
    short_c, short = run(repo, date=TUE, start=time(11), end=time(12, 30), interests=["history"])
    assert short.itinerary  # no reserve under 2 hours


def test_invalid_window_is_rejected():
    with pytest.raises(ValueError):
        PlanConstraints(date=TUE, start=time(15), end=time(11))


@pytest.mark.parametrize(
    "day, start, end, interests, settings, child",
    list(
        itertools.product(
            [MON, TUE, WED],
            [time(9), time(13)],
            [time(17)],
            [["history"], ["nature"], ["venetian fortress"], []],
            [[], [Setting.OUTDOOR]],
            [False, True],
        )
    ),
)
def test_planner_output_always_passes_validation(repo, day, start, end, interests, settings, child):
    """Planner and validator are independent; any plan must pass validation."""
    c, res = run(repo, date=day, start=start, end=end, interests=interests, settings=settings, with_child=child)
    if res.itinerary:
        assert res.validation.ok, [e.message for e in res.validation.errors]
        for s in res.itinerary.stops:
            assert open_intervals(repo.get(s.poi_id), day)
