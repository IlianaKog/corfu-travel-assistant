# Corfu Travel Assistant

A small conversational travel assistant for Corfu. It answers questions grounded in a curated 50-place dataset, recommends places from stated interests and constraints, builds realistic 3–5 hour walking itineraries, and revises them when the visitor changes their mind or the weather changes.

The guiding rule is simple: **the LLM interprets requests and phrases answers; deterministic code decides and validates.** Dates, opening hours, walking times and schedule arithmetic are never left to the model.

> The dataset is a recruitment exercise fixture. Opening hours, visit durations and family-suitability flags are scenario assumptions, not live visitor information.

See [`DESIGN.md`](DESIGN.md) for the two-page design note and [`eval/FINDINGS.md`](eval/FINDINGS.md) for what the evaluation revealed.

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

The chat UI (`frontend/index.html`) is a single page with no build step and no external scripts. It calls `POST /chat` like any other client and shows:
- the reply, with the code-rendered timetable in monospace;
- app-generated notices, such as a time the visitor never gave;
- the cited records and the weather source;
- a collapsible tool trace.

It also has buttons that fill in the brief's demo prompts, and a checkbox that injects the synthetic rain fixture for a new conversation (scenario D). If the server restarts (sessions are in memory), the page says so and continues in a new conversation.

The build downloads the embedding model (~130 MB) and builds the vector index into the image, so it needs network access once. At runtime the container needs network access only for the LLM provider and Open-Meteo. Exact dependency versions come from `requirements.lock`. The container runs as a non-root user (UID 1000) and never contains `.env`; credentials are passed at runtime.

Without compose:

```bash
docker build -t corfu-assistant .
docker run --rm -p 8000:8000 --env-file .env corfu-assistant
```

### 3. Chat from the terminal

```bash
docker compose run --rm app python -m app.cli
```

To replay the reproducible rain scenario, inject the synthetic weather fixture:

```bash
docker compose run --rm app python -m app.cli --weather-fixture data/corfu_synthetic_rain.json
```

Useful options: `--trace` prints every tool call and result; `-m "..."` sends messages non-interactively (repeat it for follow-ups in the same conversation). In the chat, `/new` starts a new conversation, `/state` shows the stored preferences and `/quit` exits.

### Without Docker (Python 3.12)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock     # Windows: .venv\Scripts\pip
cp .env.example .env                           # then set LLM_API_KEY
.venv/bin/python -m app.rag.index              # first run downloads the embedding model
.venv/bin/python -m app.cli --trace
.venv/bin/uvicorn app.api:app --port 8000      # chat UI at http://localhost:8000, REST API on the same port
```

---

## LLM provider

The assistant talks to the LLM through the **OpenAI-compatible Chat Completions API** using the `openai` Python package with a configurable `base_url`. Reviewers can use whichever provider and key they have, including free tiers and fully local models. Switching provider is a `.env` change only.

| Provider | `LLM_BASE_URL` | Notes |
|---|---|---|
| Google Gemini (default, free tier) | `https://generativelanguage.googleapis.com/v1beta/openai/` | Key from Google AI Studio |
| Groq (free tier) | `https://api.groq.com/openai/v1` | Fast; use a model with tool-calling support |
| Ollama (local, no key) | `http://host.docker.internal:11434/v1` | Fully offline; small models are less reliable at tool calling |
| OpenAI / OpenRouter / others | provider's OpenAI-compatible URL | Any endpoint supporting tool calls |

```env
# .env.example (abridged)
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=
LLM_MODEL=gemini-3.1-flash-lite   # any tool-calling model offered by the provider
LLM_TEMPERATURE=0.2
LLM_MAX_TOOL_ROUNDS=6
LLM_MAX_RETRIES=4
LOG_LEVEL=WARNING
```

**Free-tier quotas** are per model and can be small. When tested, `gemini-3.5-flash` allowed 20 requests per day and 5 per minute. Each user message takes 2–4 requests. The default `gemini-3.1-flash-lite` is the model the evaluation ran on. The client handles provider errors as follows:

| Error | Behaviour |
|---|---|
| Rate limit (HTTP 429) | Waits for the delay the provider asks for (`Retry-After` or Google's `retryDelay`), then retries |
| Daily quota exhausted | Reports it clearly instead of retrying; switch `LLM_MODEL` to another model |
| Server or connection error | Retries with exponential backoff |

The agent loop is capped at `LLM_MAX_TOOL_ROUNDS` tool rounds per user message.

---

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Required by the brief |
| API | FastAPI + Uvicorn | Small, typed, self-documenting (`/docs`) |
| Chat UI | One static HTML page with vanilla JavaScript, served by FastAPI | No build step, no extra dependency, same container and port; uses the public REST API |
| CLI | Plain `argparse`; `--trace` prints every tool call | Convenient for demos and the screen recording |
| LLM access | `openai` SDK against any OpenAI-compatible endpoint | Provider-agnostic; works with free and local models |
| Orchestration | **No framework**; a hand-written tool-calling loop (~100 lines) | See "Why no LangChain" below |
| Embeddings | `fastembed` with `BAAI/bge-small-en-v1.5` (local ONNX) | No API key or cost, no PyTorch, deterministic, baked into the image |
| Vector index | NumPy cosine similarity + lexical bonus, persisted to `.npz` | 50 documents; a vector database would add nothing but moving parts |
| Weather | Open-Meteo forecast API via `httpx` | Free, keyless, returns the same WMO weather codes as the fixture |
| Validation / models | Pydantic v2 | Typed domain objects and strict validation of tool arguments |
| Timezone | `zoneinfo` (`Europe/Athens`) | Standard library, DST-correct |
| Tests | pytest | Deterministic tests need no LLM and no network |
| Packaging | Docker (single container), docker compose | Required by the brief; retrieval storage is embedded |

### Why no LangChain

The brief allows LangChain, LlamaIndex or no framework. The hard parts of this problem are scheduling, opening-hours logic, timezone handling and weather-driven replanning. These are plain domain code, and a framework does not help with them. Retrieval over 50 single-chunk descriptions needs no loaders, splitters or vector-store abstractions. A small explicit loop keeps every prompt, tool call and tool result visible, which makes it straightforward to trace a bad itinerary.

In production the loop maps directly onto LangGraph: each tool becomes a node, and `SessionState` becomes checkpointed graph state. Because the tools are already pure functions over typed inputs, that migration would be mechanical.

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

### Tools

| Tool | Purpose | Changes state |
|---|---|---|
| `search_places_info` | RAG over descriptions for descriptive questions; returns passages with POI ids, query terms absent from the dataset, and the facts the dataset never contains | no |
| `get_place_details` | Looks up named places: fixture hours (or "unknown"), setting, duration, family flag; reports names not in the dataset | no |
| `recommend_places` | Ranks places by interests using metadata filters (no itinerary) | stores preferences |
| `get_weather` | Forecast for a date/window or the next N hours; live Open-Meteo or an injected fixture, always labelled | no |
| `plan_itinerary` | New validated plan, or a feasibility check for named places (`must_include_places`) | stores plan and preferences |
| `replace_stop` | Replaces one stop, keeping the others and their order | stores plan; persists party facts only |
| `revise_itinerary` | Applies changed constraints (window, exclusions, child, removals) with as few changes as possible: drops few stops, keeping the most relevant visiting time, then fills the freed time | stores plan and preferences |
| `adapt_plan_to_weather` | Fetches the forecast for the current plan's window itself and adapts it to rain | stores plan; later changes keep avoiding rain |

### Separation of responsibilities

| Layer | Responsible for | Not responsible for |
|---|---|---|
| **LLM** | Understanding paraphrased requests, choosing tools, extracting constraint changes, writing the final answer | Times, distances, opening-hours decisions, inventing places |
| **RAG** (`search_places_info`) | Descriptive questions ("What should I see if I like Venetian history?"); returns passages with POI ids | Filtering by hours, setting or family flags |
| **Structured retrieval** (`get_place_details`, `recommend_places`, planner candidates) | Filtering on metadata: category, tags, indoor/outdoor, family suitability, open on a given day | Free-text semantics |
| **Live tools** (`get_weather`) | Hourly forecast for the requested date and window; the result is marked `open-meteo` or `synthetic_fixture` | Interpreting what the weather means for the plan |
| **Planner / Validator** | Building, repairing and checking itineraries; explaining why something does not fit | Natural-language phrasing |
| **SessionState** | Remembering preferences, constraints and the current plan across turns; merging explicit deltas | Free-form memory of the transcript |

### Key design decisions

1. **The itinerary is rendered by code, not written by the LLM.** The LLM's answer refers to the plan, but the timetable shown to the user comes straight from the planner's output. A hallucinated time cannot reach the user.
2. **Conversation state is structured and lives on the server.** The LLM changes it only through tool arguments, for example `replace_stop(stop_number=2, with_child=true)`. Tools merge what they receive over what is stored, and every result echoes `active_constraints`. The merge rules are explicit:
   - date, times and interests are replaced only when given; a topic the visitor adds (`add_interests`) is appended in code, so the earlier ones cannot be dropped, and may swap one or two stops so the new topic appears;
   - moving only the start time keeps what the visitor stated: a duration keeps the window's length ("from 16:00" after "one hour" means 16:00–17:00), and an end time stays ("free 10:00–13:30", then "start at 10:30", means 10:30–13:30);
   - exclusions and removed places accumulate, and a replaced stop counts as removed, so a later change cannot bring it back;
   - places the visitor named are kept through later changes while they still fit, and a dropped one is reported;
   - facts about the party (a child, their age) persist;
   - once the plan has been checked against the weather, later changes (`replace_stop`, `revise_itinerary`, a new plan) fetch the forecast again and keep outdoor stops out of the rain;
   - a wish for one replacement slot, or a requested number of stops ("five museums"), applies to that request only.
3. **Not every question goes through vector search.** "Open on Tuesday", "indoor", "child-friendly" are metadata filters. Embeddings are used for descriptive relevance and to rank candidates by interest. Interests that differ only in word form or filler ("historical attractions", "historic sites", "history") are ranked as one vocabulary term, so the plan does not depend on how a model phrases them; any phrase with words outside the vocabulary ("Gorgon pediment") is kept as written. With several interests, each is scored on its own scale and the planner rewards covering each of them, so "history and views" includes waterfront stops instead of only history; a place that covers none of them is not used as a filler.
4. **Unknown is not open.** A POI with `opening_hours_fixture: null` is never scheduled and never promised as an indoor or rain shelter stop. It may be mentioned with an explicit caveat.
5. **Weather is injected through an interface.** `WeatherProvider` has two implementations: `OpenMeteoProvider` (always used for live forecasts) and `FixtureProvider` (used only when a fixture is explicitly injected for reproducible tests). The prompt never carries rain timing.
6. **Similarity scores are not treated as proof.** On this corpus cosine similarity does not separate supported from unsupported questions. "Corfu aquarium with sharks" scores 0.66 against the Byzantine Museum, higher than genuine matches. Retrieval therefore also reports query terms that appear nowhere in the dataset, and the tool layer states which facts the dataset never contains (prices, tickets, bookings, live hours). The LLM may only answer from a passage that actually states the fact.
7. **Tool arguments are validated before execution.** Invalid arguments are returned to the model as a structured error so it can correct itself. Known-harmless slips seen in the evaluation are normalised first: `"12:30,"`, `"10:00,00"`, and `"14:00:00Z"`, which is read as local wall-clock time. "Outdoor"/"indoor" given as an interest becomes a setting filter.
8. **Provider quirks are handled in the loop.** Assistant messages are replayed exactly as received, because Gemini 3 rejects tool calls without their `thought_signature`.
9. **Assumptions are disclosed by the app, not left to the model.** In testing, the model chose a start time the visitor never gave and called it "your requested time", despite a prompt rule to disclose assumptions. The plan tools therefore compare every start and end time with the visitor's own messages. A time that does not appear there produces a **notice** that the app shows verbatim under the reply (`notices` in the API). A stated duration ("one hour", "four-hour") counts for the end time. For today's date, a start time already in the past is flagged as well.
10. **Shortfalls are explained with computed numbers.** When the visitor asks for a number of stops and fewer fit, the planner returns `partial` with a lower bound, for example *"Only 1 of the 5 requested stops fit in the 60-min window … the 5 shortest matching visits need at least 290 min"*. The model relays that bound instead of estimating its own.
11. **Tool descriptions steer extraction.** The `interests` description says the visitor's topics go there in their own words; `categories` says it is too broad on its own. Without this, "museums" was sent as the category `history`, and the plan filled up with squares and gates.
12. **Formal tone.** Replies are formal and concise, with no enthusiasm phrases or exclamation marks. This is a style instruction in the system prompt; nothing that must always hold depends on it.

---

## Itinerary planning

### Walking time model

```
walk_minutes = ceil( haversine_km × 1.3 / 4.5 km/h × 60 )
```

- `1.3` is a detour factor for street layout in the Old Town (straight-line distance underestimates real routes).
- `4.5 km/h` is a relaxed sightseeing pace; with a child it drops to `3.5 km/h`.
- Any single walking leg over **3 km** (estimated route distance) is treated as not walkable. For example, Achilleion Palace is roughly 7 km in a straight line from the Old Town, so it is rejected for an on-foot plan and the reason is explained.
- Each walking leg gets a **5-minute transition buffer** (finding the entrance, tickets, orientation).
- The itinerary starts at the first stop; getting there is not counted.

These are approximations. There is no routing API, no elevation and no traffic, so the assistant always presents walking times as estimates.

### Opening hours

- Hours are interpreted in `Europe/Athens` local time.
- A `null` weekday means closed that day. A `null` whole field means unknown, which is never treated as open.
- An end time earlier than the start time (`20:00-02:00`) continues into the next calendar day.
- A stop is feasible only if the visitor **arrives after opening and finishes the full suggested visit before closing**. Waiting for a place to open is allowed for up to 30 minutes.

### Algorithm

1. **Candidates:** places that pass the hard constraints (category, exclusions, setting, family suitability) and have confirmed fixture hours.
2. **Relevance:** a weighted mix of tag/category vocabulary matches (0.6) and embedding similarity to the stated interests (0.4). With interests, places below a relevance of 0.3 are dropped, so unrelated fillers stay out. If the model sends only categories, these are used as interests.
3. **Search:** a beam search (width 60, at most 6 stops) over stop sequences. Each extension is checked for opening hours, waiting time, the maximum walking leg, the window end and, when weather is considered, rain exposure. Sequences are ranked by relevance and visit time, minus walking and waiting penalties.
4. **Reserve:** new plans of two hours or more finish at least 10 minutes early. This leaves room for delays and for later revisions such as a slower child's pace. Revisions may use the full window.
5. **Requested stop count:** a number of stops the visitor asked for is an upper limit for the search. If fewer fit, the result is `partial`, with a note giving the lower bound: the N shortest matching places open that day, plus 5-minute transitions, before any walking.
6. **Validate:** the chosen plan is re-checked by an independent validator, which recomputes everything from the dataset and produces a `ValidationReport` (checks, warnings, errors).

### Revisions

- **Replace a stop:** the other stops keep their order and the time window is kept.
  - If nothing fits in the requested position, another position is tried and the change of order is reported.
  - If nothing matching the wish fits, the closest alternative that meets the hard constraints is used, and this is stated.
  - If nothing fits at all, the plan is unchanged and the answer explains why with numbers (minutes left vs. minutes needed), including whether the current stop already meets the request.
- **Change constraints** (window, exclusions, child, removed places): stops that still satisfy the new constraints are kept where possible, and only the rest is re-planned. Moving only the start time shifts the whole window and keeps its length.
- **Weather adaptation:** hours with precipitation are identified. Outdoor and `mixed` stops scheduled in rainy hours are handled in two steps. First, the plan tries to reorder the same stops into dry hours. Otherwise, it keeps the stops before the rain and fills the rainy hours with indoor places that have confirmed hours. The original time window is preserved, and walks that fall in rain are flagged.
- **Infeasible requests** (for example five attractions in 90 minutes) are not forced. The assistant explains which constraints fail (total time, closures, distance) and proposes the largest subset that fits.

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chat UI (`frontend/index.html`) |
| `POST` | `/chat` | `{ "message", "session_id"?, "weather_fixture"? }` → answer, itinerary, `itinerary_text`, `notices` (app-generated, e.g. assumed times), sources, weather sources, tool trace, `session_id`. Omit `session_id` to start a new conversation. |
| `POST` | `/sessions` | Start a conversation explicitly (optionally with `weather_fixture`), returns `session_id` |
| `GET` | `/sessions/{id}` | Current state: preferences, itinerary, weather mode, number of turns |
| `GET` | `/health` | Liveness check; reports the configured model and whether a key is set |

Every `/chat` response includes the **tool trace** (tool name, arguments, result), so reviewers can see which data source produced each fact. `weather_fixture` accepts only a file name inside `data/`, e.g. `corfu_synthetic_rain.json`.

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
  results.md       first run: expected vs observed behaviour, generated (+ results.json, raw)
  results2.md      intermediate run: tightened cases + two added cases (+ results2.json)
  results3.md      final run: final code, 10 cases (+ results3.json)
  FINDINGS.md      analysis: observed failure, causes, proposed improvements
demo/
  run_scenarios.py executable demo of scenarios A–E (prompts verbatim from the brief)
  transcript.md    output of the last demo run, generated
DESIGN.md          two-page design note
Dockerfile, docker-compose.yml, .dockerignore
requirements.txt   direct dependencies (ranges)
requirements.lock  exact tested versions (used by the Docker image)
.env.example
```

---

## Tests, evaluation and demo

```bash
# deterministic tests: planner, validator, hours, walking times, retrieval,
# weather adaptation, tools, agent loop (scripted LLM), API
# (Open-Meteo is replayed from tests/fixtures/, no network or LLM key needed)
docker compose run --rm app pytest

# also call the real Open-Meteo API
docker compose run --rm -e RUN_LIVE=1 app pytest tests/test_weather.py

# evaluation cases against the real LLM (requires LLM_API_KEY); writes eval/results.md
# run a subset with: --only E5 E7
docker compose run --rm app python -m eval.run_eval

# demo of scenarios A–E from the brief (D uses the injected rain fixture, E uses live weather)
docker compose run --rm app python -m demo.run_scenarios            # all; --only B D for some; --trace for tool results
```

The demo sends the brief's prompts verbatim through the same agent the CLI and API use. Each scenario starts a new conversation, and the follow-ups in B and D stay in the same one. Scenario E prints the real Open-Meteo request URL and the hourly response. The run is also written to `demo/transcript.md`.

The itinerary validation and the rain-replanning branch are fully reproducible without live weather or an LLM key.

---

## Assumptions

- All times are `Europe/Athens` local wall-clock time, including times passed to tools.
- A plan needs a date and a time window. If they are missing, the tool refuses and the assistant asks instead of guessing. If the model still supplies a time the visitor never wrote, the app shows a notice that it was assumed. Relative dates ("tomorrow", "this Sunday") are resolved by the LLM from the current Athens date given in the system prompt; the tool validates the resulting date's format, not the arithmetic.
- Past dates are accepted, because the brief's scenarios are dated early October 2026. Only a start time already in the past *today* is flagged.
- Walking times are distance-based estimates (see the model above), not routed times.
- `family_suitability_fixture: true` is required for child-oriented stops. `null` is treated as not confirmed.
- `mixed` setting is not considered shelter from rain.
- Rain is considered significant at `precipitation_mm ≥ 0.5` per hour or a WMO rain/shower/thunderstorm code.
- An hourly weather value stamped `HH:00` is treated as covering `HH:00–HH+1:00`. This matches the fixture's scenario text ("rain begins after 15:00"). Open-Meteo's own precipitation values are sums over the *preceding* hour, so live data is shifted accordingly.
- Itineraries only include places with confirmed fixture hours. Places with unknown hours can be described, but are never scheduled.
- Sessions are kept in memory. They are lost when the container restarts.

## Known limitations

- No routing: walking times ignore elevation (for example the climb inside the fortresses) and road crossings.
- Time windows cannot cross midnight (end must be after start on the same day), although the opening-hours logic supports overnight hours. The assistant explains this instead of planning (evaluation case E10).
- The LLM can drop a fact the visitor stated, as in E6, where the model omitted a child's age (see `eval/FINDINGS.md` for this and later runs). It can also skip style instructions such as `[CF-xxx]` citations. Hard constraints are enforced in code; the sources are always returned in the `sources` field.
- Free-tier LLMs are rate-limited and occasionally choose the wrong tool or arguments. Validation catches bad arguments, but a poor tool choice can still produce a weaker answer.
- Live weather is only available within Open-Meteo's range (observed: about 92 days back to 15 days ahead). Outside it the provider's own error is reported as "unavailable", never guessed. Past dates return model data, not observations.
- The injected fixture covers only 2026-10-07, 13:00–17:00. Other dates or hours are reported as having no data instead of falling back to live weather, so synthetic and live data are never mixed in one answer.
- "Rain expected" is a model forecast, not a certainty. The weather tool returns hourly precipitation probability, and answers must state it.
- Interest matching relies on English descriptions and a small tag vocabulary. Requests in other languages depend on the LLM translating them into tool arguments.
- The assumed-time check recognises common formats (`15:00`, `15.00`, `3 pm`, `noon`, "one hour", "four-hour", "90 minutes", "half-day"). Unusual phrasings such as "quarter past three" or "until lunch" produce a notice even though the visitor did give a time. This errs on the side of disclosure.
- The check cannot stop the model from sending a wrong time that the visitor did write elsewhere in the conversation; it only detects times that appear nowhere.
- Session state is in-memory and single-process.
- The lexical check for query terms absent from the dataset detects missing topics (an aquarium), not missing facts about present topics. "Ticket" appears in dataset disclaimers, so a ticket-price question is not flagged lexically; it relies on the tool layer's list of facts the dataset does not contain.

See [`eval/FINDINGS.md`](eval/FINDINGS.md) for what the evaluation revealed (an observed failure, its cause and proposed improvements) and [`eval/results3.md`](eval/results3.md) for the final run (10/10). [`eval/results.md`](eval/results.md) is the first run, kept because FINDINGS analyses it.
