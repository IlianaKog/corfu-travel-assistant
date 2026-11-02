"""Executable demo of the brief's reference scenarios A-E.

    python -m demo.run_scenarios              # all scenarios
    python -m demo.run_scenarios --only B D   # selected scenarios
    python -m demo.run_scenarios --trace      # also print every tool call

The prompts are sent verbatim through the application's agent, exactly as the
CLI and the REST API do. Each scenario starts a new conversation; the follow-ups
in B and D stay in the same conversation. D injects the synthetic rain fixture
as the weather-tool response; E uses the live Open-Meteo API and prints the real
request and its response. A transcript is written to demo/transcript.md.
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import config
from app.agent import Agent, AgentError, AgentReply
from app.state import SessionStore


@dataclass
class Scenario:
    key: str
    title: str
    prompts: list[str]
    weather_fixture: Path | None = None


SCENARIOS = [
    Scenario("A", "Art & museums", [
        "Plan a four-hour visit to Corfu focused on art and museums on Monday, October 5, 2026, from 13:00 to 17:00.",
    ]),
    Scenario("B", "Historical walk + follow-up", [
        "Plan a four-hour walking itinerary in Corfu focused on historical attractions on Tuesday, October 6, 2026, from 11:00 to 15:00.",
        "Replace the second stop with something suitable for my 10-year-old daughter. Keep the rest of my itinerary and make sure we still finish by 15:00.",
    ]),
    Scenario("C", "Feasibility", [
        "Can I visit Old Fortress, New Fortress, Achilleion Palace, Mon Repos Museum of Palaiopolis, and Museum of Asian Art on foot "
        "in just 90 minutes on Tuesday, October 6, 2026, between 11:00 and 12:30? Create an itinerary if possible.",
    ]),
    Scenario("D", "Weather replanning (synthetic rain fixture injected)", [
        "Plan a four-hour itinerary in Corfu focused on outdoor historical attractions on Wednesday, October 7, 2026, from 13:00 to 17:00.",
        "Can you check the weather for Wednesday, October 7, 2026, afternoon and adapt my existing itinerary if needed "
        "while keeping the original 13:00 - 17:00 time window?",
    ], weather_fixture=config.RAIN_FIXTURE_PATH),
    Scenario("E", "Live weather (real Open-Meteo call)", [
        "What is the weather forecast for Corfu for the next 24 hours? Should I expect rain?",
    ]),
]


class Output:
    """Prints to the console and collects the same text for the transcript."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)


def show_turn(out: Output, prompt: str, reply: AgentReply, trace: bool, weather_details: bool) -> None:
    out(f"**User:** {prompt}")
    out()
    for t in reply.tool_trace:
        out(f"- tool `{t.tool}` {json.dumps(t.arguments, ensure_ascii=False)}")
        if trace:
            out(f"  - result: `{json.dumps(t.result, ensure_ascii=False, default=str)[:400]}…`")
    if reply.tool_trace:
        out()
    out("**Assistant:** " + reply.text.replace("\n", "\n> ").strip())
    out()
    if reply.itinerary_text:
        out("```")
        out(reply.itinerary_text)
        out("```")
        out()
    for notice in reply.notices:
        out(f"> **Note:** {notice}")
        out()
    meta = []
    if reply.sources:
        meta.append("records: " + ", ".join(reply.sources))
    if reply.weather_sources:
        meta.append("weather source: " + ", ".join(reply.weather_sources))
    if meta:
        out("_" + " · ".join(meta) + "_")
        out()
    if weather_details:
        shown = set()
        for t in reply.tool_trace:
            if t.tool not in ("get_weather", "adapt_plan_to_weather"):
                continue
            w = t.result.get("weather", t.result)
            key = (w.get("source"), w.get("period"), w.get("unavailable"))
            if key in shown:  # get_weather and adapt_plan_to_weather may return the same forecast
                continue
            shown.add(key)
            if w.get("request_url"):
                out(f"Real API request: `GET {w['request_url']}`")
                out()
            if w.get("unavailable"):
                out(f"Weather unavailable: {w['unavailable']}")
                continue
            out(f"Weather response ({w.get('source')}, is_live={w.get('is_live')}), {w.get('period')}:")
            out()
            out("| Hour | °C | mm | Prob. | Conditions |")
            out("|---|---|---|---|---|")
            for h in w.get("hours", []):
                prob = "" if h["precipitation_probability"] is None else f"{h['precipitation_probability']:g}%"
                out(f"| {h['time'][-5:]} | {h['temperature_c']} | {h['precipitation_mm']} | {prob} | {h['conditions']} |")
            out()
            out(f"Rain windows: {', '.join(w.get('rain_windows') or []) or 'none'} ({w.get('rain_rule')})")
            out()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the brief's demo scenarios A-E")
    parser.add_argument("--only", nargs="*", help="scenario letters, e.g. B D")
    parser.add_argument("--trace", action="store_true", help="also print tool results")
    parser.add_argument("--out", default=str(Path(__file__).parent / "transcript.md"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=config.LOG_LEVEL)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    try:
        agent = Agent()
    except AgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    wanted = {k.upper() for k in args.only} if args.only else None
    out = Output()
    out("# Demo transcript: scenarios A–E")
    out()
    out(f"Run {datetime.now(config.TZ):%Y-%m-%d %H:%M} Europe/Athens · model `{config.LLM_MODEL}` · prompts sent verbatim from the brief.")
    failures = 0
    for sc in SCENARIOS:
        if wanted and sc.key not in wanted:
            continue
        out()
        out(f"## {sc.key} / {sc.title}")
        out()
        weather = f"synthetic fixture `{sc.weather_fixture.name}` injected" if sc.weather_fixture else "live Open-Meteo"
        out(f"_New conversation · weather: {weather}_")
        out()
        session = SessionStore().create(str(sc.weather_fixture) if sc.weather_fixture else None)
        for i, prompt in enumerate(sc.prompts):
            if i:
                out("**Follow-up, same conversation**")
                out()
            try:
                reply = agent.chat(session, prompt)
            except AgentError as e:
                out(f"**User:** {prompt}")
                out()
                out(f"**Error:** {e}")
                out()
                failures += 1
                break
            show_turn(out, prompt, reply, args.trace, weather_details=sc.key in ("D", "E"))

    Path(args.out).write_text("\n".join(out.lines) + "\n", encoding="utf-8")
    print(f"\ntranscript written to {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
