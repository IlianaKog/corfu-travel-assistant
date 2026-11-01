# Design note

**Principle:** the LLM interprets requests and phrases answers; deterministic code decides and validates. Anything that must always hold (dates, opening hours, distances, schedule arithmetic, which places exist) is enforced in code. Prompt instructions are kept for tone and presentation, because an instruction raises the probability of a behaviour but does not guarantee it. Testing confirmed this: the model omitted a stated child's age (E6), sometimes skipped `[CF-xxx]` citations, and once chose a start time the visitor never gave and called it "your requested time", despite a prompt rule to disclose assumptions.

```
message ─► agent loop (LLM) ─► tool call ─► argument validation (Pydantic) ─► tool
                ▲                                                              │
                └──────────── JSON facts (status, notes, changes, ◄────────────┘
                              validation, active_constraints)
reply = LLM text + timetable rendered by code + app notices + sources (POI ids) + weather source
```

## 1. Separation of concerns

**RAG (`app/rag/`)** answers descriptive questions only.
- **Corpus and index:** one document per record (name, area, category, tags, description); one-paragraph descriptions need no chunking. Embeddings come from `bge-small-en-v1.5` via fastembed (local ONNX, no key, baked into the image) into a NumPy matrix: exact for 50 vectors, where a vector database would add a service and no accuracy.
- **Hybrid score:** cosine similarity plus a small bonus for query words in a record's name or tags. Cosine scores here are tightly bunched: Old Fortress ranked sixth for "Venetian history in Corfu".
- **Similarity is not proof:** "Corfu aquarium with sharks" scores 0.66 against the Byzantine Museum. The tool therefore also returns query terms absent from the corpus and a fixed list of facts the dataset never contains (prices, bookings, live hours). The model may only answer from a passage that states the fact.

**Structured retrieval (`app/recommend.py`).** Category, tags, indoor/outdoor, family suitability and opening hours are metadata, filtered directly, never through vector search. Interest relevance mixes a small tag vocabulary (0.6) with min-max-scaled semantic similarity (0.4), with a 0.3 threshold; at 0.15, unrelated "fillers" entered art plans. Words like "outdoor" are moved from interests to the setting filter, whichever field the model used, and phrasings that differ only in word form ("historical attractions", "history") are ranked as the same term, so two models that word an interest differently get the same plan. Several interests are scored separately and the planner rewards covering each; summed, the larger one (history) crowded out the other.

**Live tools (`app/weather.py`).** A `WeatherProvider` interface has two implementations. **Open-Meteo** is keyless and used for every real request; its precipitation values describe the *preceding* hour and are re-aligned. The **fixture provider** is used only when a fixture is injected for a session, and refuses dates it does not cover rather than falling back to live data. Every result carries `source`, `is_live` and, for live calls, the request URL. Outages and out-of-range dates return "unavailable", never a guess; missing values are stated as text, not left null.

**LLM reasoning (`app/agent.py`).** A hand-written tool-calling loop (~100 lines) over any OpenAI-compatible endpoint; Gemini is the default for its free tier. The model chooses among eight tools and writes the reply.
- **It never computes or displays times.** A duration is passed as `duration_minutes` and converted in code; the timetable is appended from planner output.
- **Assumptions are disclosed by code.** The plan tools compare each start and end time with the visitor's own messages (a stated duration counts for the end). A time that appears nowhere becomes a notice the app shows verbatim under the reply. Named places and stop counts are checked the same way: one the visitor never mentioned is ignored, because a model that adds them turns a topic request into a narrow feasibility check.
- **Extraction is steered at the point of use.** Tool descriptions say topics go in `interests` in the visitor's words. Without this, "museums" was sent as the category `history`, and the plan filled with squares and gates.
- **Provider robustness.** Assistant messages are replayed verbatim (Gemini 3 rejects tool calls without their `thought_signature`); rate limits wait for the provider's `retryDelay`; a used-up daily quota is reported, not retried; the loop is capped at six tool rounds.
- **No LangChain:** the hard parts are domain code, and an explicit loop keeps every prompt, call and result traceable.

**Itinerary validation (`app/planner.py`, `app/validator.py`).** A beam search (width 60, at most six stops) checks each extension for: the full suggested visit inside the fixture hours (Europe/Athens wall-clock time); walking legs (haversine × 1.3 at 4.5 km/h, 3.5 with a child) of at most 3 km plus a 5-minute transition; waits for opening of at most 30 minutes; the window end, with new plans of two hours or more keeping 10 minutes in reserve for delays and later revisions; and no outdoor or `mixed` stop during forecast rain. Null hours mean unknown, never open, so such places are never scheduled.

The **validator** is independent: it recomputes every check from the dataset instead of trusting the planner's times, and mutation tests confirm it rejects invented places, impossible walks, overruns and closed days. Infeasible requests are explained with arithmetic ("355 min of visits vs a 90-min window") and the largest subset that fits. When fewer stops fit than the visitor asked for, the result is `partial` with a computed lower bound ("the 5 shortest matching visits need at least 290 min"), so the model relays a number instead of estimating one.

**Conversation state (`app/state.py`).** State is structured and server-side: preferences, constraints and the current itinerary per session. Tools merge only what they are given, and every result echoes `active_constraints`:

| Field | Rule |
|---|---|
| date, times, interests | replaced only when provided; an added topic is appended in code; moving only the start keeps what the visitor stated: the duration, or else the end time |
| exclusions, removed or replaced places | accumulate, so a weak model cannot silently drop them |
| places named by the visitor | kept first in later changes; a dropped one is reported |
| party facts (child, age) | persist across turns |
| a weather check | persists: later changes re-read the forecast, so adding a child cannot put outdoor stops back into the rain |
| a wish for one replacement slot, a requested stop count | apply to that request only |

Revisions keep unaffected stops: a replacement keeps the other stops' order, a constraint change drops few stops, keeping the most relevant visiting time, and fills the freed time around the rest, and weather adaptation reorders before it substitutes, always within the original window.

## 2. Extending it safely

**Rule:** behaviour that must always hold is added as code with a test, never only as a prompt instruction. Every change is checked by the deterministic suite (284 tests, no LLM or network) and the LLM evaluation (`eval/`, 10 cases; failures and fixes in `eval/FINDINGS.md`).

- **New data or city:** replace the dataset behind `POIRepository`, which validates every record. The index rebuilds when its fingerprint (corpus + model) changes. Only the vocabulary in `recommend.py` is dataset-specific; timezone and coordinates are configuration.
- **New hard constraint** (e.g. accessibility): add the field to the dataset model and `PlanConstraints`, enforce it in `constraint_violation` (shared by planner and validator), and test it before exposing it as a tool argument.
- **New tool:** a Pydantic argument model, facts plus `status`/`notes` in the result, errors returned as data. Anything that changes the plan goes through `_plan_output`, so it is validated, stored and rendered by code.
- **Another LLM:** three environment variables. Re-run the evaluation first, since argument extraction differs by model.
- **Production hardening**, from what testing exposed: co-required schema fields (`with_child` implies `child_age`); citation post-processing in code; sessions in Redis or Postgres; windows as two datetimes so plans can cross midnight; a routing API behind the same `walk_leg` interface; and repeated evaluation runs with required pass rates before a prompt, model or tool change ships.
