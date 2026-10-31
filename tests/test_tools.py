"""Tool layer: argument validation, state merging across turns, and the brief's
multi-turn flows driven through tools directly (no LLM)."""

import json

import pytest

from app.config import RAIN_FIXTURE_PATH
from app.state import SessionState
from app.tools import Deps, openai_tool_specs, run_tool
from app.weather import FixtureProvider, WeatherUnavailable, load_fixture, overlaps


class NoWeather:
    def forecast(self, start, end):
        raise WeatherUnavailable("offline in tests")


@pytest.fixture
def deps(repo):
    def weather(fixture):
        return FixtureProvider(fixture) if fixture else NoWeather()

    return Deps(repo=repo, weather_factory=weather)


def call(state, deps, name, **args):
    return run_tool(name, json.dumps(args), state, deps)


# --- schemas ---------------------------------------------------------------------

def test_tool_schemas_use_a_portable_json_schema_subset():
    text = json.dumps(openai_tool_specs())
    assert "$ref" not in text and "$defs" not in text and '"null"' not in text
    for spec in openai_tool_specs():
        assert spec["function"]["parameters"]["type"] == "object"
    # "format": "time" means HH:MM:SS; Groq validates against it and rejected "13:00"
    assert '"format": "time"' not in text
    plan = next(s for s in openai_tool_specs() if s["function"]["name"] == "plan_itinerary")["function"]["parameters"]["properties"]
    assert plan["start_time"] == {"type": "string", "description": "HH:MM local time."}


def test_enum_fields_are_inlined():
    plan = next(s for s in openai_tool_specs() if s["function"]["name"] == "plan_itinerary")
    settings = plan["function"]["parameters"]["properties"]["settings"]
    assert settings["type"] == "array" and set(settings["items"]["enum"]) == {"indoor", "outdoor", "mixed"}


# --- validation ------------------------------------------------------------------

def test_invalid_arguments_return_an_error_the_model_can_fix(deps):
    out = run_tool("plan_itinerary", '{"date": "next tuesday", "start_time": "11:00"}', SessionState(), deps)
    assert out.data["error"] == "invalid arguments"
    assert any("date" in str(d["loc"]) for d in out.data["detail"])


@pytest.mark.parametrize(
    "raw, expected",
    [("17:00,", "17:00"), (" 9:00 ", "09:00"), ("13.30", "13:30"), ("12:30.", "12:30"),
     ("10:00,00", "10:00"), ("14:00:00", "14:00"), ("14:00:00.000Z", "14:00"), ("14:00+03:00", "14:00")],
)
def test_time_formatting_slips_are_tidied(deps, raw, expected):
    """Seen live in the eval: "12:30,", "10:00,00" (cost a retry) and "14:00:00.000Z".
    Times are local wall-clock by contract, so a 'Z' does not shift the hour."""
    out = run_tool("plan_itinerary", json.dumps({"date": "2026-10-06", "start_time": "08:00", "end_time": raw}), SessionState(), deps)
    assert "error" not in out.data
    assert out.data["itinerary"]["window"].endswith(expected)


@pytest.mark.parametrize("raw", ["half past two", "25:00", "afternoon"])
def test_unparseable_times_are_still_rejected(deps, raw):
    out = run_tool("plan_itinerary", json.dumps({"date": "2026-10-06", "start_time": "11:00", "end_time": raw}), SessionState(), deps)
    assert out.data["error"] == "invalid arguments"


def test_malformed_json_and_unknown_tool(deps):
    assert run_tool("plan_itinerary", "{not json", SessionState(), deps).data["error"] == "invalid arguments"
    assert "unknown tool" in run_tool("book_tickets", "{}", SessionState(), deps).data["error"]


def test_missing_window_asks_instead_of_guessing(deps):
    out = call(SessionState(), deps, "plan_itinerary", interests=["history"])
    assert "missing date, start, end" in out.data["error"]


def test_reversed_window_is_rejected(deps):
    out = call(SessionState(), deps, "plan_itinerary", date="2026-10-06", start_time="15:00", end_time="11:00")
    assert "must be after" in out.data["error"]


# --- grounding ---------------------------------------------------------------------

def test_search_returns_evidence_and_dataset_gaps(deps):
    out = call(SessionState(), deps, "search_places_info", query="Where is the Gorgon pediment displayed?")
    assert out.data["results"][0]["poi_id"] == "CF-031"
    assert any("ticket" in g for g in out.data["dataset_does_not_contain"])
    assert "CF-031" in out.sources


def test_details_report_unknown_hours_and_unknown_places(deps):
    out = call(SessionState(), deps, "get_place_details", places=["Saint Spyridon", "Corfu Aquarium"], date="2026-10-06")
    spyridon = out.data["places"][0]
    assert spyridon["poi_id"] == "CF-004" and spyridon["on_date"] == "unknown hours"
    assert out.data["unresolved"] == [{"name": "Corfu Aquarium", "problem": "not in the dataset"}]


def test_fabricated_place_in_plan_is_not_substituted(deps):
    out = call(SessionState(), deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00",
               must_include_places=["Old Fortress", "Corfu Aquarium"])
    assert out.data["status"] == "needs_clarification"
    assert not out.itinerary_changed


# --- scenario B through tools: plan, then replace stop 2 for a child -----------------

def test_historical_walk_then_child_replacement(deps):
    s = SessionState()
    first = call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", duration_minutes=240, interests=["historical attractions"])
    assert first.itinerary_changed and first.data["itinerary"]["window"] == "11:00-15:00"  # end computed in code
    before = s.itinerary.poi_ids()

    second = call(s, deps, "replace_stop", stop_number=2, interests=["outdoor"], with_child=True, child_age=10)
    after = s.itinerary.poi_ids()
    assert second.data["status"] == "ok"
    kept = [i for i in before if i != before[1]]
    assert [i for i in after if i in kept] == kept  # every other stop kept, same relative order
    (new_id,) = set(after) - set(before)
    assert deps.repo.get(new_id).family_suitability_fixture is True
    if after.index(new_id) != 1:  # slower child pace may force another slot; it must be disclosed
        assert any("order changed" in c for c in second.data["changes"])
    assert s.itinerary.finish.strftime("%H:%M") <= "15:00"
    # party facts persist; the slot-only wish ("outdoor") does not become a global constraint
    assert s.preferences.with_child and s.preferences.child_age == 10
    assert s.preferences.settings == []
    assert s.preferences.interests == ["historical attractions"]


@pytest.mark.parametrize(
    "interests, expected_interests, expected_settings",
    [
        (["outdoor historical attractions"], ["historical attractions"], ["outdoor"]),
        (["outdoor"], [], ["outdoor"]),
        (["indoor museums", "art"], ["museums", "art"], ["indoor"]),
        (["venetian history"], ["venetian history"], []),
    ],
)
def test_setting_words_in_interests_become_settings(interests, expected_interests, expected_settings):
    from app.recommend import split_setting_words

    rest, settings = split_setting_words(interests)
    assert rest == expected_interests and [s.value for s in settings] == expected_settings


def test_categories_without_interests_still_rank_by_topic(deps):
    """Seen live: the model sent categories=['history'] and no interests."""
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", categories=["history"])
    with_interests = SessionState()
    call(with_interests, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00",
         categories=["history"], interests=["history"])
    assert s.itinerary.poi_ids() == with_interests.itinerary.poi_ids()


def test_outdoor_wish_in_interests_filters_by_setting(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["outdoor historical attractions"])
    assert [x.value for x in s.preferences.settings] == ["outdoor"]
    assert all(st.setting.value == "outdoor" for st in s.itinerary.stops)


def test_requested_stop_count_shortfall_is_explained_with_numbers(deps):
    """Seen live: "five museums in one hour" returned 1 museum with status ok and no
    note, so the model explained the shortfall from its own reasoning."""
    s = SessionState()
    out = call(s, deps, "plan_itinerary", date="2026-10-30", start_time="10:00", duration_minutes=60,
               interests=["museums"], max_stops=5)
    assert out.data["status"] == "partial"
    notes = " ".join(out.data["notes"])
    assert "Only 1 of the 5 requested stops fit in the 60-min window" in notes
    assert "need at least" in notes and "before walking" in notes
    # the count belongs to that request only, not to the stored preferences
    later = call(s, deps, "plan_itinerary", date="2026-10-30", start_time="10:00", end_time="14:00", interests=["museums"])
    assert later.data["status"] == "ok" and not any("requested stops" in n for n in later.data["notes"])


def test_requested_count_that_fits_has_no_shortfall_note(deps):
    out = call(SessionState(), deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00",
               interests=["history"], max_stops=3)
    assert out.data["status"] == "ok" and len(out.data["itinerary"]["stops"]) == 3


def test_past_start_time_today_is_flagged(deps, monkeypatch):
    """Seen live: given only a date, the model picked 14:30 for today on its own."""
    from app import tools
    from app.config import TZ
    from datetime import datetime

    monkeypatch.setattr(tools, "now_local", lambda: datetime(2026, 10, 30, 14, 41, tzinfo=TZ))
    out = call(SessionState(), deps, "plan_itinerary", date="2026-10-30", start_time="14:30", duration_minutes=60, interests=["museums"])
    assert out.data["current_time_in_corfu"] == "14:41"
    assert "already in the past" in out.data["start_time_warning"]
    # within tolerance, or a future start: no warning
    ok = call(SessionState(), deps, "plan_itinerary", date="2026-10-30", start_time="14:40", duration_minutes=60, interests=["museums"])
    assert "start_time_warning" not in ok.data
    # past dates are not flagged: the brief's demo scenarios use them
    past_day = call(SessionState(), deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", interests=["history"])
    assert "start_time_warning" not in past_day.data and "current_time_in_corfu" not in past_day.data


def said(*texts) -> SessionState:
    return SessionState(messages=[{"role": "user", "content": t} for t in texts])


def test_unstated_start_time_becomes_an_app_notice(deps):
    """Seen live: given only '30/10', the model chose 15:00 and called it the visitor's time."""
    s = said("I want to see 5 museums in one hour", "30/10")
    out = call(s, deps, "plan_itinerary", date="2026-10-30", start_time="15:00", duration_minutes=60,
               interests=["museums"], max_stops=5)
    assert len(out.notices) == 1 and "start time 15:00 was not stated" in out.notices[0]  # "one hour" covers the end
    assert out.data["assumed_times"] == out.notices


@pytest.mark.parametrize(
    "texts, start, end",
    [
        (["Plan a four-hour walking itinerary on Tuesday, October 6, 2026, from 11:00 to 15:00."], "11:00", "15:00"),
        (["Can I visit them between 11:00 and 12:30 on 6 October 2026?"], "11:00", "12:30"),
        (["keeping the original 13:00 - 17:00 time window"], "13:00", "17:00"),
        (["a four-hour visit on 6 Oct 2026 starting at 3 pm"], "15:00", "19:00"),
        (["history walk on 6 Oct 2026", "from 9.30, for 90 minutes"], "09:30", "11:00"),
    ],
)
def test_stated_times_raise_no_notice(deps, texts, start, end):
    out = call(said(*texts), deps, "plan_itinerary", date="2026-10-06", start_time=start, end_time=end, interests=["history"])
    assert out.notices == [] and "assumed_times" not in out.data


def test_places_and_stop_count_the_visitor_never_gave_are_ignored(deps):
    """Seen live (gpt-oss-120b): a topic request was turned into a two-stop feasibility check."""
    s = said("Plan a four-hour itinerary in Corfu focused on Venetian history and fortresses on Wednesday, October 7, 2026, from 13:00 to 17:00.")
    out = call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00",
               interests=["Venetian history", "fortresses"], must_include_places=["Old Fortress", "New Fortress"], max_stops=3)
    assert s.preferences.named_places == []
    assert len(out.data["ignored_arguments"]) == 3 and len(s.itinerary.stops) > 3  # an ordinary plan, not a 2-place check


@pytest.mark.parametrize("text, args, ids", [
    ("Can we walk to the Liston, the Old Fortress and Town Hall Square on 8 October 2026, 10:00-13:30?",
     ["Liston Arcades", "Old Fortress", "Town Hall Square"], {"CF-032", "CF-001", "CF-016"}),
    ("Squeeze the Archaeological Museum, the Byzantine Museum, Casa Parlante, the Asian Art museum and the Old Fortress "
     "into 8 October 2026 from 10:00 to 13:30.", ["CF-031", "CF-029", "CF-007", "CF-017", "CF-001"], {"CF-031", "CF-029", "CF-007", "CF-017", "CF-001"}),
])
def test_places_the_visitor_named_are_recognised(deps, text, args, ids):
    s = said(text)
    out = call(s, deps, "plan_itinerary", date="2026-10-08", start_time="10:00", end_time="13:30", must_include_places=args)
    assert set(s.preferences.named_places) == ids and "ignored_arguments" not in out.data


def test_stated_stop_count_is_kept(deps):
    out = call(said("I want to see five museums on 30/10 from 16:00 for one hour"), deps, "plan_itinerary",
               date="2026-10-30", start_time="16:00", duration_minutes=60, interests=["museums"], max_stops=5)
    assert "ignored_arguments" not in out.data and out.data["status"] == "partial"


def test_unstated_end_time_is_flagged(deps):
    out = call(said("history walk on 6 Oct 2026 from 11:00"), deps, "plan_itinerary",
               date="2026-10-06", start_time="11:00", end_time="15:00", interests=["history"])
    assert len(out.notices) == 1 and "end time 15:00 (240-minute window) was not stated" in out.notices[0]


def test_moving_only_the_start_keeps_the_duration(deps):
    """Seen live: "from 16:00" after a one-hour plan became a two-hour window."""
    s = said("5 museums in one hour on 30/10 from 15:00", "from 16:00")
    call(s, deps, "plan_itinerary", date="2026-10-30", start_time="15:00", duration_minutes=60, interests=["museums"])
    out = call(s, deps, "revise_itinerary", start_time="16:00")
    assert out.data["itinerary"]["window"] == "16:00-17:00"
    assert out.notices == []  # 16:00 was said, and "one hour" covers the end


def test_tool_schema_steers_topics_to_interests():
    plan = next(t for t in openai_tool_specs() if t["function"]["name"] == "plan_itinerary")["function"]["parameters"]["properties"]
    assert "Museums and galleries are not a category" in plan["interests"]["description"]
    assert "Too broad on its own" in plan["categories"]["description"]


def test_weather_tools_steer_away_from_a_redundant_forecast_call():
    specs = {t["function"]["name"]: t["function"]["description"] for t in openai_tool_specs()}
    assert "do not call get_weather first" in specs["adapt_plan_to_weather"]
    assert "which adapt_plan_to_weather does alone" in specs["get_weather"]


def test_revise_keeps_stops_and_persists_exclusions(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", interests=["history"])
    out = call(s, deps, "revise_itinerary", exclude_tags=["museum"], end_time="14:00")
    assert out.data["status"] == "ok"
    assert s.itinerary.finish.strftime("%H:%M") <= "14:00"
    assert all("museum" not in deps.repo.get(i).tags for i in s.itinerary.poi_ids())
    assert s.preferences.exclude_tags == ["museum"] and s.preferences.interests == ["history"]

    removed = s.itinerary.poi_ids()[0]
    call(s, deps, "revise_itinerary", remove_places=[removed])
    call(s, deps, "revise_itinerary", end_time="15:00")
    assert removed not in s.itinerary.poi_ids()  # removal survives later revisions


def test_fresh_start_clears_old_preferences(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", interests=["history"], with_child=True)
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="10:00", end_time="13:00", interests=["nature"], fresh_start=True)
    assert s.preferences.interests == ["nature"] and not s.preferences.with_child


# --- scenario C and D through tools -------------------------------------------------

def test_feasibility_named_places(deps):
    out = call(SessionState(), deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="12:30",
               must_include_places=["Old Fortress", "New Fortress", "Achilleion Palace", "Mon Repos Museum of Palaiopolis", "Museum of Asian Art"])
    assert out.data["status"] == "partial"
    assert any("355 min" in n for n in out.data["notes"])


def test_weather_adaptation_uses_injected_fixture(deps):
    s = SessionState(weather_fixture=str(RAIN_FIXTURE_PATH))
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["historical attractions"], settings=["outdoor"])
    out = call(s, deps, "adapt_plan_to_weather")
    assert out.weather_source == "synthetic_fixture"
    assert out.data["weather"]["rain_windows"] == ["2026-10-07 15:00-17:00"]
    assert out.data["status"] == "ok" and out.data["validation"]["ok"]
    assert out.data["itinerary"]["window"] == "13:00-17:00"


def test_adaptation_gives_the_model_the_same_weather_facts_as_get_weather(deps):
    s = SessionState(weather_fixture=str(RAIN_FIXTURE_PATH))
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["history"], settings=["outdoor"])
    adapted = call(s, deps, "adapt_plan_to_weather").data["weather"]
    asked = call(s, deps, "get_weather", date="2026-10-07", start_time="13:00", end_time="17:00").data
    assert adapted == asked  # one adapt call is enough: same summary, source URL and guidance
    assert "precipitation probability" in adapted["guidance"] and "request_url" in adapted


def _rain_exposed(itin):
    rain = load_fixture().rain_windows()
    return [s.name for s in itin.stops if s.setting.value != "indoor" and overlaps(rain, s.start, s.end)]


@pytest.mark.parametrize("change, args", [
    ("revise_itinerary", {"with_child": True, "child_age": 8}),
    ("replace_stop", {"stop_number": 1, "with_child": True, "child_age": 8}),
])
def test_later_changes_keep_the_weather_adaptation(deps, change, args):
    # Seen in testing: adding a child after adapt_plan_to_weather put outdoor stops back into the rain.
    s = SessionState(weather_fixture=str(RAIN_FIXTURE_PATH))
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["venetian history", "fortresses"])
    call(s, deps, "adapt_plan_to_weather")
    assert s.preferences.consider_weather and not _rain_exposed(s.itinerary)
    out = call(s, deps, change, **args)
    assert out.data["status"] == "ok" and out.data["validation"]["ok"]
    assert out.weather_source == "synthetic_fixture"
    assert not _rain_exposed(s.itinerary)
    changed = s.itinerary.poi_ids()[:1] if change == "replace_stop" else s.itinerary.poi_ids()  # replace_stop keeps the other stops
    assert all(deps.repo.get(i).family_suitability_fixture is True for i in changed)


def test_indoor_stops_kept_by_the_rain_survive_an_outdoor_preference(deps):
    s = SessionState(weather_fixture=str(RAIN_FIXTURE_PATH))
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["history"], settings=["outdoor"])
    call(s, deps, "adapt_plan_to_weather")
    out = call(s, deps, "revise_itinerary", with_child=True)
    assert not any("'indoor' not allowed" in n for n in out.data["notes"])
    assert "indoor" in [st.setting.value for st in s.itinerary.stops] and not _rain_exposed(s.itinerary)


def test_a_replaced_stop_does_not_come_back(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", interests=["history"])
    replaced = s.itinerary.poi_ids()[1]
    call(s, deps, "replace_stop", stop_number=2)
    call(s, deps, "revise_itinerary", end_time="14:30")
    assert replaced in s.preferences.excluded_poi_ids and replaced not in s.itinerary.poi_ids()


def test_an_earlier_end_drops_as_few_stops_as_possible(deps):
    """Seen live: ending 30 min earlier rebuilt the plan and dropped the Old Fortress for two short stops."""
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-06", start_time="11:00", end_time="15:00", interests=["historical attractions"], categories=["history"])
    call(s, deps, "replace_stop", stop_number=2)
    before = s.itinerary.poi_ids()
    out = call(s, deps, "revise_itinerary", end_time="14:30")
    after = s.itinerary.poi_ids()
    assert "CF-001" in after and len(set(before) - set(after)) == 1  # one stop dropped, the Old Fortress kept
    assert [i for i in before if i in after] == after  # the kept stops stay in their order
    assert out.data["validation"]["ok"] and s.itinerary.finish.strftime("%H:%M") <= "14:30"


def test_places_named_by_the_visitor_survive_later_changes(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-08", start_time="10:00", end_time="13:30",
         must_include_places=["Liston Arcades", "Old Fortress", "Town Hall Square"])
    out = call(s, deps, "revise_itinerary", with_child=True)
    # Old Fortress is not confirmed as family-suitable: dropped, and said so once
    assert [n for n in out.data["notes"] if "Old Fortress" in n] == ["Old Fortress no longer fits the constraints: not confirmed as family-suitable in the dataset."]
    call(s, deps, "revise_itinerary", start_time="10:30")
    assert {"CF-032", "CF-016"} <= set(s.itinerary.poi_ids())


def test_naming_a_place_again_lifts_its_exclusion(deps):
    s = SessionState()
    call(s, deps, "plan_itinerary", date="2026-10-08", start_time="10:00", end_time="13:30", interests=["history"])
    call(s, deps, "revise_itinerary", remove_places=["Old Fortress"])
    assert "CF-001" in s.preferences.excluded_poi_ids
    call(s, deps, "plan_itinerary", must_include_places=["Old Fortress"])
    assert "CF-001" not in s.preferences.excluded_poi_ids and "CF-001" in s.itinerary.poi_ids()


def test_weather_unavailable_keeps_plan(deps):
    s = SessionState()  # live provider replaced by NoWeather
    call(s, deps, "plan_itinerary", date="2026-10-07", start_time="13:00", end_time="17:00", interests=["history"])
    before = s.itinerary.poi_ids()
    out = call(s, deps, "adapt_plan_to_weather")
    assert out.data["status"] == "weather_unavailable" and s.itinerary.poi_ids() == before


def test_revision_tools_need_an_existing_plan(deps):
    for name, args in [("replace_stop", {"stop_number": 1}), ("revise_itinerary", {}), ("adapt_plan_to_weather", {})]:
        assert "no itinerary" in call(SessionState(), deps, name, **args).data["error"]
