"""Terminal chat.

    python -m app.cli                      # live weather
    python -m app.cli --weather-fixture data/corfu_synthetic_rain.json
    python -m app.cli --trace              # also print tool calls

Commands: /new (new conversation), /state (show stored preferences), /quit
"""

import argparse
import json
import logging
import sys

from app import config
from app.agent import Agent, AgentError, AgentReply
from app.state import SessionStore


def print_reply(reply: AgentReply, trace: bool) -> None:
    if trace:
        for t in reply.tool_trace:
            print(f"  [tool] {t.tool}({json.dumps(t.arguments, ensure_ascii=False)})")
            print("         -> " + json.dumps(t.result, ensure_ascii=False, default=str)[:600])
    print(f"\nassistant> {reply.text}")
    if reply.itinerary_text:
        print("\n" + reply.itinerary_text)
    for notice in reply.notices:
        print(f"\n  Note: {notice}")
    meta = []
    if reply.sources:
        meta.append("records: " + ", ".join(reply.sources))
    if reply.weather_sources:
        meta.append("weather: " + ", ".join(reply.weather_sources))
    for t in reply.tool_trace:
        if url := t.result.get("request_url"):
            meta.append(f"weather API call: GET {url}")
    if meta:
        print("  (" + " | ".join(meta) + ")")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corfu travel assistant (terminal chat)")
    parser.add_argument("--weather-fixture", help="inject a recorded weather response (reproducible tests only)")
    parser.add_argument("--trace", action="store_true", help="print tool calls and results")
    parser.add_argument("-m", "--message", action="append", help="send message(s) non-interactively, in order, in one conversation")
    args = parser.parse_args(argv)
    logging.basicConfig(level=config.LOG_LEVEL)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    try:
        agent = Agent()
    except AgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    store = SessionStore()
    session = store.create(args.weather_fixture)
    weather = "synthetic fixture " + args.weather_fixture if args.weather_fixture else "live Open-Meteo"
    print(f"Corfu travel assistant · weather: {weather} · /new /state /quit")

    def send(text: str) -> None:
        print(f"you> {text}")
        try:
            print_reply(agent.chat(session, text), args.trace)
        except AgentError as e:
            print(f"error: {e}\n")

    if args.message:
        for m in args.message:
            send(m)
        return 0

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/new":
            session = store.create(args.weather_fixture)
            print("(new conversation)\n")
            continue
        if text == "/state":
            print(json.dumps(session.preferences.describe(), indent=2, ensure_ascii=False))
            continue
        try:
            print_reply(agent.chat(session, text), args.trace)
        except AgentError as e:
            print(f"error: {e}\n")


if __name__ == "__main__":
    sys.exit(main())
