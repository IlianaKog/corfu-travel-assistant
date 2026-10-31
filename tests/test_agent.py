"""Agent loop mechanics with a scripted fake LLM client (no network, no key)."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletionMessage

from app.agent import Agent, AgentError, history_window, retry_delay
from app.state import SessionState
from app.tools import Deps
from tests.test_tools import NoWeather


def tool_call(name, args, call_id="c1", signature=None):
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    if signature:  # Gemini 3 returns this and requires it back on the next request
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def response(content=None, calls=None):
    msg = ChatCompletionMessage.model_validate({"role": "assistant", "content": content, "tool_calls": calls})
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def status_error(cls, code, body=None, headers=None):
    resp = httpx.Response(code, headers=headers or {}, request=httpx.Request("POST", "http://llm"))
    return cls("error", response=resp, body=body)


class ScriptedClient:
    """Returns the scripted responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def deps(repo):
    return Deps(repo=repo, weather_factory=lambda fixture: NoWeather())


def test_plan_turn_appends_code_rendered_timetable(deps):
    client = ScriptedClient(
        response(calls=[tool_call("plan_itinerary", {"date": "2026-10-06", "start_time": "11:00", "end_time": "15:00", "interests": ["history"]})]),
        response(content="Here is a historical walk."),
    )
    s = SessionState()
    reply = Agent(deps, client=client).chat(s, "Plan a history walk on 6 Oct 2026, 11-15")
    assert reply.text == "Here is a historical walk."
    assert reply.itinerary is not None and reply.itinerary_text.startswith("Itinerary for Tuesday 06 October 2026")
    assert reply.sources == reply.itinerary.poi_ids()
    # the tool result was sent back to the model with the matching call id
    tool_msgs = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"


def test_assumed_time_notice_reaches_the_reply_without_the_llm(deps):
    client = ScriptedClient(
        response(calls=[tool_call("plan_itinerary", {"date": "2026-10-30", "start_time": "15:00", "duration_minutes": 60, "interests": ["museums"]})]),
        response(content="Here is your requested plan from 15:00."),  # the model claims the time was requested
    )
    reply = Agent(deps, client=client).chat(SessionState(), "museums for one hour on 30/10")
    assert reply.notices and "15:00 was not stated" in reply.notices[0]


def test_invalid_tool_arguments_are_fed_back_and_corrected(deps):
    client = ScriptedClient(
        response(calls=[tool_call("plan_itinerary", {"date": "next tuesday"})]),
        response(calls=[tool_call("plan_itinerary", {"date": "2026-10-06", "start_time": "11:00", "end_time": "15:00"}, "c2")]),
        response(content="Done."),
    )
    reply = Agent(deps, client=client).chat(SessionState(), "plan something")
    assert reply.tool_trace[0].result["error"] == "invalid arguments"
    assert reply.itinerary is not None


def test_question_without_tools_has_no_itinerary(deps):
    client = ScriptedClient(response(content="Which date are you visiting?"))
    reply = Agent(deps, client=client).chat(SessionState(), "plan a day for me")
    assert reply.itinerary is None and reply.text.endswith("?")


def test_tool_round_limit(deps, monkeypatch):
    monkeypatch.setattr("app.config.LLM_MAX_TOOL_ROUNDS", 2)
    loop = response(calls=[tool_call("get_place_details", {"places": ["Old Fortress"]})])
    reply = Agent(deps, client=ScriptedClient(loop, loop)).chat(SessionState(), "x")
    assert "could not finish" in reply.text


def test_conversation_history_and_system_prompt(deps):
    client = ScriptedClient(response(content="Hi"), response(content="Again"))
    agent, s = Agent(deps, client=client), SessionState()
    agent.chat(s, "first")
    agent.chat(s, "second")
    msgs = client.requests[1]["messages"]
    assert msgs[0]["role"] == "system" and "Europe/Athens" in msgs[0]["content"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["first", "second"]


def test_fixture_mode_is_announced_to_the_model(deps):
    client = ScriptedClient(response(content="ok"))
    Agent(deps, client=client).chat(SessionState(weather_fixture="x.json"), "hi")
    assert "SYNTHETIC" in client.requests[0]["messages"][0]["content"]


def test_provider_errors_become_readable(deps):
    with pytest.raises(AgentError, match="rejected the API key"):
        Agent(deps, client=ScriptedClient(status_error(openai.AuthenticationError, 401))).chat(SessionState(), "hi")


def test_thought_signature_is_sent_back(deps):
    client = ScriptedClient(
        response(calls=[tool_call("get_place_details", {"places": ["Old Fortress"]}, signature="SIG123")]),
        response(content="The Old Fortress [CF-001] ..."),
    )
    Agent(deps, client=client).chat(SessionState(), "tell me about the old fortress")
    replayed = next(m for m in client.requests[1]["messages"] if m["role"] == "assistant")
    assert replayed["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG123"


def test_rate_limit_waits_as_instructed_then_succeeds(deps):
    body = [{"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}}]
    waits = []
    client = ScriptedClient(status_error(openai.RateLimitError, 429, body=body), response(content="ok"))
    reply = Agent(deps, client=client, sleep=waits.append).chat(SessionState(), "hi")
    assert reply.text == "ok" and waits == [13]


def test_daily_quota_is_reported_without_waiting(deps):
    body = [{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}, {"retryDelay": "51180s"}]}}]
    with pytest.raises(AgentError, match="daily request quota"):
        Agent(deps, client=ScriptedClient(status_error(openai.RateLimitError, 429, body=body)), sleep=lambda s: None).chat(SessionState(), "hi")


def test_rate_limit_with_long_wait_gives_up(deps):
    err = status_error(openai.RateLimitError, 429, headers={"retry-after": "3600"})
    with pytest.raises(AgentError, match="rate limit"):
        Agent(deps, client=ScriptedClient(err), sleep=lambda s: None).chat(SessionState(), "hi")


def test_retry_delay_parsing():
    assert retry_delay(status_error(openai.RateLimitError, 429, headers={"retry-after": "7"})) == 7
    assert retry_delay(status_error(openai.RateLimitError, 429, body={"retryDelay": "55.5s"})) == 55.5
    assert retry_delay(status_error(openai.RateLimitError, 429)) is None


def test_history_window_never_starts_with_a_tool_result():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "tool", "content": "{}"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert history_window(msgs, limit=3)[0]["role"] == "user"
