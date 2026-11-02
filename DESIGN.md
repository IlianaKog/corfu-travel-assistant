# Design note

**Principle:** the LLM interprets requests and phrases answers; deterministic code decides and validates. Anything that must always hold (dates, opening hours, distances, schedule arithmetic, which places exist) is enforced in code, because a prompt instruction raises the probability of a behaviour but does not guarantee it. The evaluation showed this repeatedly (`eval/FINDINGS.md`).

```
message ─► agent loop (LLM) ─► tool call ─► argument validation (Pydantic) ─► tool
                ▲                                                              │
                └──────────── JSON facts (status, notes, changes, ◄────────────┘
                              validation, active_constraints)
reply = LLM text + timetable rendered by code + app notices + sources (POI ids) + weather source
```

## 1. Separation of concerns

**RAG (`app/rag/`)** answers descriptive questions ("where is the Gorgon pediment?"). One embedding index serves two uses: retrieval for descriptive answers (below), and the semantic part of interest relevance in recommendations and planning (see Structured retrieval).
1. **Ingestion:** each record becomes one document (name, area, category, tags, description). Descriptions are one paragraph, so there is no chunking. The documents are embedded with `bge-small-en-v1.5` (fastembed, local ONNX, no key) into a NumPy matrix stored in `storage/`. A fingerprint of corpus and model triggers a rebuild when either changes. For 50 vectors an exact search is instant, and a vector database would add a service but no accuracy.
2. **Retrieval:** the query is embedded and scored by cosine similarity, plus a small bonus per query word found in a record's name or tags (cosine scores here are tightly bunched). The top 4 passages above 0.5 are returned with their POI ids. A category filter can be applied first.
3. **Grounded answer:** the model answers only from a passage that states the fact, citing `[CF-xxx]`. The record ids are also returned by code in `sources`. Similarity is not proof: "Corfu aquarium with sharks" scores 0.66 against the Byzantine Museum. The tool therefore also returns the query terms absent from the corpus, and a fixed list of facts the dataset never contains (prices, bookings, live hours).

**Structured retrieval (`app/recommend.py`).** Category, tags, indoor/outdoor, family suitability and opening hours are metadata, filtered directly, never through vector search. Relevance to an interest combines a tag vocabulary (0.6) with the same embeddings, scaled per query (0.4). Places below 0.3 are not candidates. Equivalent phrasings ("historical attractions", "history") are ranked as one term, so the plan does not depend on a model's wording. Several interests are scored separately, and a place counts as covering an interest at 0.5.

**Live tools (`app/weather.py`).** A `WeatherProvider` interface has two implementations. Open-Meteo is keyless and serves every real request; its precipitation values describe the preceding hour and are re-aligned. The fixture provider is used only when a fixture is injected for a session, and refuses dates it does not cover instead of falling back to live data. Every result carries `source`, `is_live` and the request URL. Outages return "unavailable", never a guess.

**LLM reasoning (`app/agent.py`).** A hand-written tool-calling loop (~100 lines) over any OpenAI-compatible endpoint. The model chooses among eight tools and writes the reply, but never computes or displays times: durations are converted in code and the timetable comes from planner output. Arguments are steered by the tool descriptions (topics go in `interests` in the visitor's words; a child goes in `with_child`; a stop count only if the visitor gave one) and validated by Pydantic; known-harmless slips such as `"12:30,"` or `"14:00Z"` are normalised, and "outdoor" given as an interest becomes a setting filter. Where the model has erred, code checks its arguments against the visitor's own messages. A start or end time the visitor never gave becomes a notice shown verbatim, and named places or stop counts the visitor never mentioned are ignored. Assistant messages are replayed verbatim (Gemini 3 requires its `thought_signature`), and rate limits wait for the provider's `retryDelay`. **No LangChain:** the hard parts are domain code, and an explicit loop keeps every call traceable.

**Planner and validator (`app/planner.py`, `app/validator.py`).** A new plan is built in four steps:
1. **Candidates:** places that pass the hard filters and have confirmed hours. Null hours mean unknown, never open.
2. **Search:** a beam search grows plans one stop at a time, up to six stops, and keeps the 60 best partial plans after each step. A stop is added only if it passes four checks:
   - the full suggested visit fits inside the fixture hours (Europe/Athens), after a wait of at most 30 minutes;
   - the walking leg (haversine × 1.3 at 4.5 km/h, 3.5 with a child) is at most 3 km, plus a 5-minute transition;
   - the plan ends inside the window; new plans of two hours or more keep 10 minutes in reserve for delays and later changes;
   - with weather considered, no outdoor or `mixed` visit falls in rain.
3. **Value:** for each stop, 10 × relevance + 0.08 per visit minute, minus 0.15 per walking minute and 0.10 per waiting minute, plus 8 per interest the plan covers.
4. **Validation:** the validator recomputes every check from the dataset instead of trusting the planner's times. Mutation tests confirm it rejects invented places, impossible walks, overruns and closed days.

Infeasible requests are explained with the planner's arithmetic ("355 min of visits vs a 90-min window") and the largest subset that fits. A requested stop count that does not fit returns `partial` with a computed lower bound.

**Revisions** change only what the request requires:
- **Replace a stop:** the other stops keep their order.
- **Change a constraint:** keep as many current stops as still fit (named places first), choosing what to drop by relevant visiting time (relevance × minutes), then fill the freed time around them. An added interest may swap up to two stops, and a note says when it is already covered or cannot be.
- **Weather:** reorder the same stops into dry hours before substituting indoor places, always within the original window.

A known gap: new plans and revisions optimise different measures, so "history and views" and "history, then also views" can differ (FINDINGS 2i).

**Conversation state (`app/state.py`).** State is structured and server-side. Tools merge only what they are given, and every result echoes `active_constraints`:

| Field | Rule |
|---|---|
| date, times, interests | replaced only when given; an added topic is appended in code; moving only the start keeps the stated duration, or else the stated end |
| exclusions, removed or replaced places | accumulate, so a weak model cannot silently drop them |
| places named by the visitor | kept first in later changes; a dropped one is reported |
| party facts (child, age), a weather check | persist; later changes re-read the forecast |
| a wish for one replacement slot, a requested stop count | apply to that request only |

## 2. Extending it safely

**Rule:** behaviour that must always hold is added as code with a test, never only as a prompt instruction. Every change is checked by the deterministic suite (284 tests, no LLM or network) and the LLM evaluation (`eval/`, 10 cases; failures and fixes in `eval/FINDINGS.md`).

- **New data or city:** replace the dataset behind `POIRepository`, which validates every record; the index rebuilds from its fingerprint. Only the vocabulary in `recommend.py` is dataset-specific.
- **New hard constraint** (e.g. accessibility): add it to the dataset model and `PlanConstraints`, enforce it in `constraint_violation` (shared by planner and validator), and test it before exposing it as a tool argument.
- **New tool:** a Pydantic argument model and facts plus `status`/`notes` in the result. Anything that changes the plan goes through `_plan_output`, so it is validated, stored and rendered by code.
- **Another LLM:** three environment variables, then re-run the evaluation, since argument extraction differs by model.
- **Production hardening:** sessions in Redis or Postgres; windows as two datetimes so plans can cross midnight; a routing API behind the same `walk_leg` interface; one value measure for new plans and revisions; repeated evaluation runs with required pass rates before a change ships.
