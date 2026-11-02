# Evaluation findings: open issues

The evaluation has 10 cases (`cases.py`). Each runs in a new conversation against the real LLM, and its checks are deterministic: they look at tool calls, stored session state and the independently validated itinerary, not at wording. The final run is [`results_final.md`](results_final.md): `gemini-3.1-flash-lite`, 2026-11-02, **10/10 cases, 56/56 checks**.

Problems found during development and since fixed in code are not listed here. Each fix has a test, and those tests describe the failure they guard against. This file keeps only what is still open: what happens, why, and the proposed improvement.

## 1. The model still puts the child in the replacement topic (E6)

**What happens.** For *"please swap the second stop for something he'd enjoy"*, the model sends `with_child: true` and `child_age: 7` correctly. It also adds a topic for the replacement, such as `interests: ["parks and gardens for children"]` (final run) or `["child-friendly park or garden"]` (an earlier run). The `replace_stop` description says that child suitability goes in `with_child`, not in the topic.

**Why it matters little here, and when it would matter.** The child is already a hard filter, so only family-suitable places are considered. In E6 the invented topic matches the plan's topic (parks), so the result is right. In a history plan, a topic such as "something for children" would rank the replacement by that phrase instead of by history, and the new stop would drift off the plan's theme.

**Cause.** This is a prompt-level instruction, and the model does not always follow it. The visitor's wording ("something he'd enjoy") invites a topic.

**Proposed improvement.** Handle it in code, as "outdoor" given as an interest already is: remove child words (child, kids, son, daughter, family) from a replacement topic and set `with_child` instead; if nothing is left, use the plan's topic.

## 2. Time windows cannot cross midnight (E10)

**What happens.** *"A night out from 21:00 to 01:00"* is not planned. `PlanConstraints` requires the end to be after the start on the same day. The model tried `end_time: "01:00"` and then a 240-minute duration, and validation rejected both. The reply explains the limitation and offers a window ending at 23:59, with no invented plan.

**Cause.** The window is a date plus two times. The data would support the request: bars open 20:00–02:00, and `hours.py` already handles overnight opening hours.

**Proposed improvement.** Represent the window as two datetimes. The scheduler would mostly work unchanged.

## 3. A new plan and a revision can choose differently for the same interests

**What happens.** *"History and views"* planned from scratch includes Faliraki Waterfront and the Old Port. *"History"*, then *"also views"*, keeps the history plan, because the Old Fortress is already tagged as a viewpoint. The revision says so in a note.

**Cause.** Part of this is intended: a revision changes only what it must, as the brief asks ("preserve … unaffected stops where they still apply"). Part of it is not. New plans maximise the value function, which pays each stop for relevance regardless of its length. Revisions compare relevant visiting time (relevance × minutes). The two parts optimise different measures.

**Proposed improvement.** Use relevant visiting time in the value function for new plans too, then re-check scenarios A–E. This changes every new plan, so it was not done close to submission.

## 4. Different models send different arguments, so plans differ

**What happens.** The planner is deterministic, but the arguments come from the model. Equivalent wordings ("historical", "historical attractions") are already ranked alike. Arguments that differ in substance still change the plan: an added setting, an unrequested interest, or an optional field filled by one model and not by another. On `openai/gpt-oss-120b` (Groq) the suite first passed 8/10, against 9/10 on Gemini, before the tool descriptions were tightened.

**Cause.** Argument extraction is model-specific. A model that fills optional fields generously changes behaviour as much as one that omits them.

**Proposed improvement.** Re-run the evaluation before changing models, and keep moving rules that must hold from descriptions into code (as was done for unrequested places, stop counts and assumed times).

## 5. Relevance is approximate

**What happens.** In E5 (*"Venetian stuff and good views"*), the first plan included the Byzantine Museum. Its relevance comes from embedding similarity to "history", not from a Venetian connection.

**Cause.** Relevance mixes a small tag vocabulary (0.6) with embedding similarity (0.4). Short stops have near-equal scores, so the choice among them is sensitive to small differences.

**Proposed improvement.** A richer tag vocabulary per place, or embeddings per tag rather than per description.

## 6. Limits of this evaluation

- **Sample size.** The suite ran several times on two models, but each result is still one run per case. The same message has produced different arguments across runs, so a case that passed once may fail on another run. Repeated runs with a pass rate per case would be more informative.
- **Provider availability.** In an earlier Gemini run, E3 and E8 ended in "provider unavailable" errors after the retries, so their checks did not run. Both passed in the final run. Free-tier quotas and Groq's tokens-per-minute limit also slow the suite.
- **Lenient text checks.** E2 and E9 use regex patterns for refusals. They catch obvious failures, not subtle hallucinations in otherwise correct-looking text.
- **Live weather.** E8 depends on the real forecast, so its content cannot be reproduced. Its checks assert the live source, the recorded API URL and a reported probability.
