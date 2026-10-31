"""REST API with a scripted LLM (no network)."""

import pytest
from fastapi.testclient import TestClient

from app import api
from app.agent import Agent
from app.tools import Deps
from tests.test_agent import ScriptedClient, response, tool_call
from tests.test_tools import NoWeather


@pytest.fixture
def client(repo, monkeypatch):
    scripted = ScriptedClient(
        response(calls=[tool_call("plan_itinerary", {"date": "2026-10-06", "start_time": "11:00", "end_time": "15:00", "interests": ["history"]})]),
        response(content="Here is your walk."),
        response(content="You are welcome."),
    )
    agent = Agent(Deps(repo=repo, weather_factory=lambda f: NoWeather()), client=scripted)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    monkeypatch.setattr(api, "store", api.SessionStore())
    return TestClient(api.app)


def test_root_serves_chat_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Corfu Travel Assistant" in r.text and 'fetch("/chat"' in r.text


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and "model" in body


def test_chat_creates_session_and_keeps_state(client):
    first = client.post("/chat", json={"message": "history walk on 6 Oct 2026, 11-15"}).json()
    sid = first["session_id"]
    assert first["itinerary"]["stops"] and first["itinerary_text"].startswith("Itinerary for")
    assert first["tool_trace"][0]["tool"] == "plan_itinerary"

    second = client.post("/chat", json={"session_id": sid, "message": "thanks"}).json()
    assert second["session_id"] == sid and second["itinerary"] is None

    state = client.get(f"/sessions/{sid}").json()
    assert state["turns"] == 2 and state["preferences"]["interests"] == ["history"]
    assert state["itinerary"]["stops"]


def test_fixture_injection_is_restricted_to_data_dir(client):
    ok = client.post("/sessions", json={"weather_fixture": "corfu_synthetic_rain.json"}).json()
    assert ok["weather"] == "synthetic_fixture"
    assert client.post("/sessions", json={"weather_fixture": "../.env"}).status_code == 400
    assert client.post("/sessions", json={"weather_fixture": "missing.json"}).status_code == 404


def test_unknown_session(client):
    assert client.post("/chat", json={"session_id": "nope", "message": "hi"}).status_code == 404
    assert client.get("/sessions/nope").status_code == 404
