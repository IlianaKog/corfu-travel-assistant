"""REST API.  Run:  uvicorn app.api:app --host 0.0.0.0 --port 8000"""

import logging
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import config
from app.agent import Agent, AgentError, AgentReply
from app.state import SessionState, SessionStore

logging.basicConfig(level=config.LOG_LEVEL)
app = FastAPI(title="Corfu Travel Assistant", version="0.1.0")
store = SessionStore()


@lru_cache(maxsize=1)
def get_agent() -> Agent:
    return Agent()


def resolve_fixture(name: str | None) -> str | None:
    """Only files inside DATA_DIR may be injected, by bare file name."""
    if not name:
        return None
    if Path(name).name != name or not name.endswith(".json"):
        raise HTTPException(400, "weather_fixture must be a .json file name inside the data directory")
    path = config.DATA_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"weather fixture '{name}' not found")
    return str(path)


class NewSession(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{}, {"weather_fixture": "corfu_synthetic_rain.json"}]}}

    weather_fixture: str | None = Field(default=None, description="e.g. corfu_synthetic_rain.json, for reproducible tests only")


class ChatRequest(BaseModel):
    """Swagger pre-fills "Try it out" with these examples instead of "string"
    placeholders, which would fail as an unknown session id / fixture name."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"message": "Plan a 3-hour history walk on Tuesday, October 6, 2026, from 10:00"},
                {"session_id": "<session_id from the previous response>", "message": "No museums please, and we must finish by 12:30."},
                {"message": "Plan an outdoor historical walk on Wednesday, October 7, 2026, 13:00-17:00", "weather_fixture": "corfu_synthetic_rain.json"},
            ]
        }
    }

    session_id: str | None = Field(default=None, description="Omit on the first message; send the id returned by the previous response to continue the conversation.")
    message: str = Field(min_length=1, max_length=4000)
    weather_fixture: str | None = Field(default=None, description="Optional. 'corfu_synthetic_rain.json' injects the synthetic rain fixture (reproducible tests only); omit for live weather.")


class ChatResponse(AgentReply):
    session_id: str


CHAT_PAGE = config.ROOT_DIR / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
def chat_ui() -> FileResponse:
    """Chat UI (frontend/index.html). It only calls POST /chat, like any other client."""
    return FileResponse(CHAT_PAGE, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": config.LLM_MODEL, "llm_key_configured": bool(config.LLM_API_KEY)}


@app.post("/sessions")
def create_session(body: NewSession | None = None) -> dict:
    s = store.create(resolve_fixture(body.weather_fixture if body else None))
    return {"session_id": s.id, "weather": "synthetic_fixture" if s.weather_fixture else "live"}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    s = _session(session_id)
    return {
        "session_id": s.id,
        "preferences": s.preferences.describe(),
        "itinerary": s.itinerary.model_dump(mode="json") if s.itinerary else None,
        "weather": "synthetic_fixture" if s.weather_fixture else "live",
        "turns": sum(1 for m in s.messages if m["role"] == "user"),
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    fixture = resolve_fixture(req.weather_fixture)
    s = _session(req.session_id) if req.session_id else store.create(fixture)
    if req.weather_fixture:
        s.weather_fixture = fixture
    try:
        reply = get_agent().chat(s, req.message)
    except AgentError as e:
        raise HTTPException(502, str(e)) from e
    return ChatResponse(session_id=s.id, **reply.model_dump())


def _session(session_id: str) -> SessionState:
    s = store.get(session_id)
    if s is None:
        raise HTTPException(404, "unknown session_id")
    return s
