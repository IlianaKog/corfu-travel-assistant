# Corfu Travel Assistant

A small conversational travel assistant for Corfu. It answers questions grounded in a curated 50-place dataset, recommends places from stated interests and constraints, builds realistic 3–5 hour walking itineraries, and revises them when the visitor changes their mind or the weather changes.

The guiding rule: **the LLM interprets requests and phrases answers; deterministic code decides and validates.** Dates, opening hours, walking times and schedule arithmetic are never left to the model.

> The dataset is a recruitment exercise fixture. Opening hours, visit durations and family-suitability flags are scenario assumptions, not live visitor information.

**For reviewers:**
- **Scenarios A–E** run with one command (`python -m demo.run_scenarios`, see [Tests, evaluation and demo](#tests-evaluation-and-demo)). The latest run is in [`demo/transcription3.md`](demo/transcription3.md).
- **Design note:** [`DESIGN.md`](DESIGN.md), two pages.
- **Evaluation:** [`eval/FINDINGS.md`](eval/FINDINGS.md) explains what the evaluation revealed. [`eval/results3.md`](eval/results3.md) is the final run (10/10).

---

## Quick start

### 1. Credentials

```bash
cp .env.example .env
# edit .env and set LLM_API_KEY (see "LLM provider" below)
```

No key is needed for weather (Open-Meteo is keyless) or embeddings (computed locally).

### 2. Run with Docker

```bash
docker compose up --build
```

Open **`http://localhost:8000`** for the chat UI. The REST API is on the same port, with interactive docs at `/docs`.

The chat UI is a single static page. It shows:
- the reply, with the timetable rendered by code;
- notices the app adds, such as a time the visitor never gave;
- the cited records and the weather source;
- a collapsible tool trace.

It has buttons for the brief's demo prompts, and a checkbox that injects the synthetic rain fixture for a new conversation (scenario D).

The build downloads the embedding model (~130 MB) and builds the vector index into the image. At runtime the container needs network access only for the LLM provider and Open-Meteo. It runs as a non-root user and never contains `.env`.

### 3. Chat from the terminal

```bash
docker compose run --rm app python -m app.cli                    # add --trace to print tool calls
docker compose run --rm app python -m app.cli --weather-fixture data/corfu_synthetic_rain.json
```

In the chat, `/new` starts a new conversation, `/state` shows the stored preferences and `/quit` exits.

### Without Docker (Python 3.12)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock     # Windows: .venv\Scripts\pip
cp .env.example .env                           # then set LLM_API_KEY
.venv/bin/uvicorn app.api:app --port 8000      # chat UI at http://localhost:8000
```

---

## LLM provider

The assistant uses the `openai` Python package against any **OpenAI-compatible** endpoint, so switching provider is a `.env` change only.

| Provider | `LLM_BASE_URL` | Notes |
|---|---|---|
| Google Gemini (default, free tier) | `https://generativelanguage.googleapis.com/v1beta/openai/` | Key from Google AI Studio. The evaluation ran on `gemini-3.1-flash-lite`. |
| Groq (free tier) | `https://api.groq.com/openai/v1` | Tested with `openai/gpt-oss-120b`. |
| Ollama (local, no key) | `http://host.docker.internal:11434/v1` | Small models are less reliable at tool calling. |

```env
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=
LLM_MODEL=gemini-3.1-flash-lite
```

**Free-tier quotas** are per model and can be small; each user message takes 2–4 requests. Rate limits are retried after the delay the provider asks for. A used-up daily quota is reported instead of retried.

---

## Tech stack

| Concern | Choice |
|---|---|
| API and chat UI | FastAPI + Uvicorn; one static HTML page, no build step |
| LLM access | `openai` SDK, any OpenAI-compatible endpoint |
| Orchestration | No framework: a hand-written tool-calling loop (reasons in [`DESIGN.md`](DESIGN.md)) |
| Embeddings | `fastembed` with `BAAI/bge-small-en-v1.5` (local, no key) |
| Vector index | NumPy cosine similarity with a lexical bonus, saved to `.npz` |
| Weather | Open-Meteo via `httpx`; an injectable fixture for reproducible tests |
| Models and validation | Pydantic v2 |
| Tests | pytest (no LLM, no network) |
| Packaging | Docker, docker compose |

---

## Architecture

```
                 ┌──────────────────────────┐
  user message ─►│ Chat UI · FastAPI · CLI  │  session_id
                 └────────────┬─────────────┘
                              ▼
                 ┌──────────────────────────┐      system prompt:
                 │  Agent loop (LLM)        │◄──── grounding rules, cite POI ids,
                 │  interpret · call tools  │      never invent facts or times
                 │  · phrase the answer     │
                 └────────────┬─────────────┘
                              │ tool calls (arguments validated by Pydantic)
     ┌──────────────────┬─────┴──────────────┬───────────────────────────────┐
     ▼                  ▼                    ▼                               ▼
 search_places_info  get_place_details   get_weather              plan_itinerary
 (RAG)               recommend_places        │                    replace_stop
     │               (structured filters)    │                    revise_itinerary
     │                  │                    │                    adapt_plan_to_weather
     ▼                  ▼                    ▼                               ▼
 ┌─────────┐      ┌──────────┐      ┌──────────────┐      ┌─────────────────────────────┐
 │ Vector  │      │ POI      │      │ Weather      │      │ Planner + Validator         │
 │ index   │      │ repo     │      │ provider     │      │ (deterministic)             │
 │ (.npz)  │      │ (JSON)   │      │ ├ Open-Meteo │      │ · walking time from coords  │
 └─────────┘      └──────────┘      │ │  (live)    │      │ · opening hours, Athens tz  │
                                    │ └ Fixture    │      │ · time window & durations   │
                                    │   (injected) │      │ · rain exposure             │
                                    └──────────────┘      │ → ValidationReport          │
                                                          └──────────────┬──────────────┘
                                                                         ▼
                                                        ┌──────────────────────────────┐
                                                        │ SessionState                 │
                                                        │ preferences · constraints ·  │
                                                        │ current itinerary · history  │
                                                        └──────────────────────────────┘
                              ▼
     response = LLM text + itinerary table rendered by code + sources (POI ids) + weather source
```

The diagram shows each tool's main data source. The planner tools also use the vector index for interest relevance and the weather provider for rain.

### Tools

| Tool | Purpose | Changes state |
|---|---|---|
| `search_places_info` | RAG over descriptions; returns passages with POI ids and the facts the dataset never contains | no |
| `get_place_details` | Looks up named places: fixture hours (or "unknown"), setting, duration, family flag | no |
| `recommend_places` | Ranks places by interests with metadata filters, open on the visit date (no itinerary) | stores preferences |
| `get_weather` | Forecast for a date and window, or the next N hours; always labelled live or synthetic | no |
| `plan_itinerary` | New validated plan, or a feasibility check for named places | stores plan and preferences |
| `replace_stop` | Replaces one stop, keeping the others and their order | stores plan |
| `revise_itinerary` | Applies changed constraints or an added interest with as few changes as possible | stores plan and preferences |
| `adapt_plan_to_weather` | Fetches the forecast for the plan's window and adapts the plan to rain | stores plan |

How the work is split between the LLM, retrieval, the planner and the session state, the planning algorithm, and the reasons behind each decision are in [`DESIGN.md`](DESIGN.md). In short, the validator checks walking distance (haversine × 1.3), opening hours in Europe/Athens, the time window and rain exposure.

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chat UI |
| `POST` | `/chat` | `{ "message", "session_id"?, "weather_fixture"? }` → reply, itinerary, `itinerary_text`, `notices`, sources, weather sources, tool trace, `session_id`. Omit `session_id` to start a new conversation. |
| `POST` | `/sessions` | Start a conversation, optionally with `weather_fixture`; returns `session_id` |
| `GET` | `/sessions/{id}` | Current preferences, itinerary and weather mode |
| `GET` | `/health` | Liveness; reports the configured model and whether a key is set |

`weather_fixture` accepts only a file name inside `data/`, e.g. `corfu_synthetic_rain.json`.

---

## Project layout

```
app/
  config.py        settings from environment
  models.py        dataset models (POI, Setting, Category)
  itinerary.py     PlanConstraints, Itinerary, Stop, ValidationReport, PlanResult
  data.py          dataset loader, POI repository, place-name matching
  geo.py           haversine and walking-time model
  hours.py         opening-hours parsing, Europe/Athens handling
  recommend.py     hard-constraint filters and interest scoring
  rag/
    ingest.py      turns POI records into retrievable documents
    index.py       embeddings + NumPy vector index (build, save, hybrid search)
  weather.py       WeatherProvider, OpenMeteoProvider, FixtureProvider
  planner.py       beam search, replace / revise / weather adaptation, feasibility
  validator.py     independent hard-constraint checks
  state.py         SessionState, preferences and their merge rules
  tools.py         tool argument models and implementations exposed to the LLM
  agent.py         tool-calling loop, system prompt, provider error handling
  render.py        timetable text rendered from planner output
  api.py           FastAPI app (also serves the chat UI at /)
  cli.py           terminal chat client
frontend/
  index.html       chat UI: one static page, calls POST /chat
data/
  corfu_dataset.json
  corfu_synthetic_rain.json
tests/             deterministic tests (no LLM, no network)
  fixtures/        recorded Open-Meteo response, replayed in tests
eval/
  cases.py         10 evaluation cases with deterministic checks
  run_eval.py      runs the cases against the real LLM
  results3.md      final run (+ results3.json, raw); results.md and results2.md are earlier runs
  FINDINGS.md      what the evaluation revealed: failures, causes, fixes, proposed improvements
demo/
  run_scenarios.py executable demo of scenarios A–E (prompts verbatim from the brief)
  transcription3.md  latest demo run, generated
DESIGN.md          two-page design note
Dockerfile, docker-compose.yml, .dockerignore
requirements.txt   direct dependencies (ranges)
requirements.lock  exact tested versions (used by the Docker image)
.env.example
```

---

## Tests, evaluation and demo

```bash
# deterministic tests (no LLM key or network; Open-Meteo is replayed from tests/fixtures/)
docker compose run --rm app pytest

# evaluation cases against the real LLM; writes eval/results.md (--only E5 E7 for a subset)
docker compose run --rm app python -m eval.run_eval

# scenarios A–E with the brief's prompts verbatim; writes demo/transcript.md
docker compose run --rm app python -m demo.run_scenarios      # --only B D for some, --trace for tool results
```

In the demo, each scenario starts a new conversation and the follow-ups in B and D stay in it. D uses the injected rain fixture; E calls the live Open-Meteo API and prints the request URL. Itinerary validation and the rain-replanning branch are reproducible without live weather or an LLM key.

---

## Assumptions

- All times are `Europe/Athens` local time. A plan needs a date and a time window; if they are missing the assistant asks, and a time the visitor never wrote is shown as an assumed-time notice.
- Walking times are estimates from coordinates, not routed times. A plan starts at the first stop; getting there is not counted.
- Only places with confirmed fixture hours are scheduled. Unknown hours are never treated as open, and `null` family suitability is treated as not confirmed.
- Rain means at least 0.5 mm in an hour or a WMO rain code. Outdoor and `mixed` stops are not scheduled during rain.
- Past dates are accepted, because the brief's scenarios are dated October 2026.

## Known limitations

- No routing: walking times ignore elevation and road crossings.
- Time windows cannot cross midnight. The assistant explains this instead of planning (evaluation case E10).
- Live weather is limited to Open-Meteo's range (observed: about 92 days back to 15 days ahead); outside it the forecast is reported as unavailable. The injected fixture covers only 2026-10-07, 13:00–17:00, and is never mixed with live data.
- The assumed-time check recognises common formats (`15:00`, `3 pm`, `noon`, "one hour", "half-day"). An unusual phrasing such as "quarter past three" produces a notice although the visitor gave a time, which errs on the side of disclosure.
- The check for query terms absent from the dataset finds missing topics (an aquarium), not missing facts about present ones. "Ticket" appears in dataset disclaimers, so a price question relies on the tool's list of facts the dataset never contains.
- The LLM can still drop a stated fact or choose a weaker tool call. Hard constraints are enforced in code; [`eval/FINDINGS.md`](eval/FINDINGS.md) records the cases seen and what was fixed.
- Interest matching relies on English descriptions and a small tag vocabulary.
- Sessions are in memory and single-process; they are lost on restart.
