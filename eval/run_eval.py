"""Run the evaluation cases against the real LLM and write eval/results.md.

    python -m eval.run_eval                 # all cases
    python -m eval.run_eval --only E5 E7    # selected cases (prefix match)
    python -m eval.run_eval --only E3 --append   # run E3 and merge it into the existing results
    python -m eval.run_eval --quiet         # one line per case instead of the conversation

Each case starts a new conversation. As in the demo, the console shows each turn
(message, tool calls, reply, timetable) and then every check. Results include
the input, expected behaviour, observed reply, tools called, final itinerary
and check outcomes.
"""

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from app import config
from app.agent import Agent, AgentError
from app.data import get_repository
from app.render import itinerary_table
from app.state import SessionStore
from demo.run_scenarios import show_turn
from eval.cases import CASES, Case, Ctx

OUT_DIR = Path(__file__).parent
# One timestamp per invocation, so cases from one run share it and only cases appended later differ.
RUN_STARTED = f"{datetime.now(config.TZ):%Y-%m-%d %H:%M}"


def console(text: str = "") -> None:
    print(text, flush=True)


def run_case(agent: Agent, case: Case, verbose: bool = False) -> dict:
    store = SessionStore()
    session = store.create(case.weather_fixture)
    replies, error = [], None
    started = time.time()
    try:
        for i, turn in enumerate(case.turns):
            replies.append(agent.chat(session, turn))
            if verbose:
                if i:
                    console("**Follow-up, same conversation**")
                    console()
                show_turn(console, turn, replies[-1], trace=False, weather_details=True)
    except AgentError as e:
        error = str(e)
    except Exception:  # record, don't abort the whole run
        error = traceback.format_exc(limit=3)
    ctx = Ctx(replies=replies, state=session, repo=get_repository())

    checks = []
    for label, fn in case.checks:
        if error and not replies:
            checks.append({"check": label, "passed": False, "detail": "not run: provider error"})
            continue
        try:
            checks.append({"check": label, "passed": bool(fn(ctx))})
        except Exception as e:
            checks.append({"check": label, "passed": False, "detail": f"{type(e).__name__}: {e}"})

    return {
        "id": case.id, "category": case.category, "turns": case.turns, "expected": case.expected, "notes": case.notes,
        "run_at": RUN_STARTED, "model": config.LLM_MODEL,
        "weather_fixture": Path(case.weather_fixture).name if case.weather_fixture else None,
        "error": error, "seconds": round(time.time() - started, 1),
        "observed": [
            {"user": t, "assistant": r.text, "tools": [{"tool": x.tool, "arguments": x.arguments} for x in r.tool_trace],
             "sources": r.sources, "weather_sources": r.weather_sources}
            for t, r in zip(case.turns, replies)
        ],
        "final_itinerary": itinerary_table(session.itinerary) if session.itinerary else None,
        "final_preferences": session.preferences.describe(),
        "checks": checks,
        "passed": not error and all(c["passed"] for c in checks),
    }


def to_markdown(results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    runs = sorted({r.get("run_at", "") for r in results} - {""})
    models = sorted({r.get("model") or config.LLM_MODEL for r in results})
    when = runs[0] if len(runs) == 1 else f"{runs[0]} to {runs[-1]} (cases added later were run separately)" if runs else \
        f"{datetime.now(config.TZ):%Y-%m-%d %H:%M}"
    lines = [
        "# Evaluation results",
        "",
        f"Run {when} Europe/Athens · model {', '.join(f'`{m}`' for m in models)} · "
        f"**{passed}/{len(results)} cases passed all checks**.",
        "",
        "Each case is a new conversation against the real LLM. Checks are deterministic assertions over tool calls, "
        "stored state and the validated itinerary; a few lenient text patterns cover refusals. "
        "LLM output varies between runs, so a single run is a sample, not a guarantee.",
        "",
        "| Case | Category | Result | Checks |",
        "|---|---|---|---|",
    ]
    for r in results:
        ok = sum(c["passed"] for c in r["checks"])
        status = "PASS" if r["passed"] else ("ERROR" if r["error"] else "FAIL")
        lines.append(f"| [{r['id']}](#{r['id'].lower()}) | {r['category']} | {status} | {ok}/{len(r['checks'])} |")
    for r in results:
        lines += ["", f"## {r['id']}", "", f"**Category:** {r['category']}" + (f" · weather fixture `{r['weather_fixture']}`" if r["weather_fixture"] else "")
                  + (f" · run {r['run_at']}" if r.get("run_at") else ""), ""]
        lines += ["**Input**", ""] + [f"{i}. \"{t}\"" for i, t in enumerate(r["turns"], 1)]
        lines += ["", f"**Expected:** {r['expected']}", ""]
        if r["notes"]:
            lines += [f"**Notes:** {r['notes']}", ""]
        lines += ["**Observed**", ""]
        for i, o in enumerate(r["observed"], 1):
            tools = ", ".join(f"`{t['tool']}({json.dumps(t['arguments'], ensure_ascii=False)})`" for t in o["tools"]) or "none"
            lines += [f"- Turn {i} tools: {tools}"]
            lines += ["", "  > " + o["assistant"].replace("\n", "\n  > "), ""]
        if r["error"]:
            lines += ["", "```", r["error"].strip(), "```", ""]
        if r["final_itinerary"]:
            lines += ["```", r["final_itinerary"], "```", ""]
        lines += ["**Checks**", ""] + [f"- {'✅' if c['passed'] else '❌'} {c['check']}" + (f" ({c['detail']})" if c.get("detail") else "") for c in r["checks"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="case numbers or ids to run, e.g. E5 E7")
    parser.add_argument("--out", default=str(OUT_DIR / "results.md"))
    parser.add_argument("--append", action="store_true", help="merge into the existing results (same id replaced) instead of overwriting")
    parser.add_argument("--quiet", action="store_true", help="one line per case instead of the conversation")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    # "E1" selects E1-grounded only, not E10 as a plain prefix match would
    cases = [c for c in CASES if not args.only or any(c.id == p or c.id.startswith(p + "-") for p in args.only)]
    agent = Agent()
    console(f"# Evaluation · model `{config.LLM_MODEL}` · {len(cases)} case(s)")
    results = []
    for case in cases:
        if args.quiet:
            console(f"running {case.id} ...")
        else:
            console()
            console(f"## {case.id} / {case.category}")
            console()
            console(f"_New conversation · weather: {'synthetic fixture injected' if case.weather_fixture else 'live Open-Meteo'}_")
            console(f"_Expected: {case.expected}_")
            console()
        r = run_case(agent, case, verbose=not args.quiet)
        results.append(r)
        if r["error"]:
            console(f"**Error:** {r['error'].splitlines()[0]}")
            console()
        if args.quiet:
            for c in r["checks"]:
                if not c["passed"]:
                    console(f"    ✗ {c['check']} {c.get('detail', '')}")
        else:
            console("**Checks**")
            for c in r["checks"]:
                console(f"  {'✓' if c['passed'] else '✗'} {c['check']}" + (f" ({c['detail']})" if c.get("detail") else ""))
        console(f"  {'PASS' if r['passed'] else 'ERROR' if r['error'] else 'FAIL'} in {r['seconds']}s")

    raw = Path(args.out).with_suffix(".json")
    if args.append and raw.exists():
        merged = {r["id"]: r for r in json.loads(raw.read_text(encoding="utf-8"))}
        merged.update({r["id"]: r for r in results})
        order = [c.id for c in CASES]
        results = sorted(merged.values(), key=lambda r: order.index(r["id"]) if r["id"] in order else len(order))
    Path(args.out).write_text(to_markdown(results), encoding="utf-8")
    raw.write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed · written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
