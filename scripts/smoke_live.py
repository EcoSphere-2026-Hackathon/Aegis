#!/usr/bin/env python3
"""
Live voice smoke test: everything a machine can prove, then a trace for the
one thing it cannot.

The microphone loop is the only part of AEGIS that no automated check can
close. A person has to speak and a person has to listen. What this script
does is remove the guesswork from either side of that: it proves every stage
up to the microphone by itself, then watches the event stream while you talk
and reports, per turn, exactly how far the utterance travelled.

    python scripts/smoke_live.py                 # preflight only
    python scripts/smoke_live.py --watch         # preflight, then trace live turns

Preflight covers configuration, token issuance, session start, agent join and
speech delivery. ``--watch`` subscribes to the same event stream the console
uses and prints one block per turn: which uid it was attributed to, whether
it was a self-echo, what claims came out, what the risk engine said, and
whether the governor spoke. If a stage is missing, the block shows where the
turn stopped.

Nothing here reports a stage it did not observe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

import httpx  # noqa: E402

from backend.agora.sessions import VoiceSessionManager  # noqa: E402
from backend.common.config import load_config  # noqa: E402
from backend.common.errors import AegisError, InterventionError  # noqa: E402
from backend.common.logging import configure_logging  # noqa: E402

configure_logging("CRITICAL")

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)
OK = f"{GREEN}ok{RESET}"
BAD = f"{RED}failed{RESET}"


def stage(name: str, ok: bool, detail: str = "") -> bool:
    print(f"    {name:<22} {OK if ok else BAD}  {DIM}{detail}{RESET}")
    return ok


# -- preflight -----------------------------------------------------------


def preflight(base_url: str) -> bool:
    """Everything provable without a human in the room."""
    print(f"\n  {BOLD}Preflight{RESET}  {DIM}no microphone required{RESET}\n")
    config = load_config()
    good = True

    good &= stage("credentials", config.agora.is_authenticated,
                  "Customer ID / Secret")
    good &= stage("token issuer", config.agora.can_issue_client_tokens,
                  "App ID / App Certificate")
    if not good:
        print(f"\n  {RED}Configuration is incomplete; nothing below can run.{RESET}\n")
        return False

    try:
        health = httpx.get(f"{base_url}/api/health", timeout=10).json()
        good &= stage("backend", health.get("status") == "ok",
                      f"extractor={health.get('extraction_provider')}")
        good &= stage("agora configured", bool(health.get("agora_authenticated")),
                      "as the server sees it")
    except Exception as exc:  # noqa: BLE001
        stage("backend", False, f"{type(exc).__name__}: is it running?")
        return False

    manager = VoiceSessionManager(config.agora)
    session = None
    try:
        session, tokens, _ = manager.start(
            incident_id=config.incident_id, participant_uid="900002"
        )
        stage("voice session", True, f"agent {session.agent_id}")
        stage("client tokens", bool(tokens.rtc_token and tokens.rtm_token),
              f"{len(tokens.rtc_token)} chars, expire {tokens.expires_at:%H:%M}")
        time.sleep(2)
        try:
            manager.speak(config.incident_id, "AEGIS live smoke test.")
            stage("speech delivery", True, "accepted by Agora")
        except (AegisError, InterventionError) as exc:
            good &= stage("speech delivery", False, str(exc))
    except AegisError as exc:
        good &= stage("voice session", False, str(exc))
    finally:
        if session is not None:
            try:
                manager.stop(session.session_id, session.participant_uid)
            except AegisError:
                pass
        manager.close()

    return bool(good)


# -- live trace ----------------------------------------------------------


class TurnTrace:
    """What one utterance was observed to do, stage by stage."""

    def __init__(self, turn_id: str, uid: str, text: str) -> None:
        self.turn_id = turn_id
        self.uid = uid
        self.text = text
        self.claims: list[str] = []
        self.verdicts: list[str] = []
        self.spoken: list[str] = []
        self.suppressed: list[str] = []
        self.resolution: Optional[str] = None

    def render(self, agent_uid: str) -> str:
        who = f"{RED}AEGIS ITSELF{RESET}" if self.uid == agent_uid else f"uid {self.uid}"
        out = [f"\n  {BOLD}turn {self.turn_id}{RESET}  {DIM}from {who}{RESET}",
               f"    said        {self.text[:88]}"]

        def line(label: str, values: list, empty: str) -> str:
            if values:
                return f"    {label:<11} {GREEN}{', '.join(values)[:88]}{RESET}"
            return f"    {label:<11} {DIM}{empty}{RESET}"

        out.append(line("claims", self.claims, "none extracted"))
        out.append(line("risk", self.verdicts, "no verdict"))
        if self.resolution:
            out.append(f"    {'resolution':<11} {GREEN}{self.resolution}{RESET}")
        out.append(line("spoke", self.spoken, "silent"))
        if self.suppressed:
            out.append(f"    {'held back':<11} {YELLOW}{', '.join(self.suppressed)}{RESET}")
        return "\n".join(out)


def watch(base_url: str, seconds: int) -> int:
    """Trace live turns off the same event stream the console reads."""
    config = load_config()
    agent_uid = config.agora.agent_uid

    print(f"\n  {BOLD}Watching for {seconds}s{RESET}  "
          f"{DIM}join voice in the console and speak{RESET}")
    print(f"  {DIM}AEGIS speaks as uid {agent_uid}; turns from it must be rejected."
          f"{RESET}\n")

    turns: dict[str, TurnTrace] = {}
    order: list[str] = []
    current: Optional[str] = None
    deadline = time.time() + seconds

    try:
        with httpx.stream("GET", f"{base_url}/api/events", timeout=None) as response:
            for raw in response.iter_lines():
                if time.time() > deadline:
                    break
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    payload = json.loads(raw[5:].strip())
                except json.JSONDecodeError:
                    continue

                kind = payload.get("kind")
                if kind == "transcript":
                    tid = str(payload.get("turn_id"))
                    trace = TurnTrace(tid, str(payload.get("uid")), payload.get("text", ""))
                    turns[tid] = trace
                    order.append(tid)
                    current = tid
                elif kind == "claim" and current:
                    turns[current].claims.append(
                        f"{payload.get('type')}"
                        + (f"({payload.get('target_ref')})" if payload.get("target_ref") else "")
                    )
                elif kind == "risk_verdict" and current:
                    turns[current].verdicts.append(str(payload.get("risk_tier")))
                elif kind == "resolution" and current:
                    turns[current].resolution = (
                        f"{payload.get('status')} {payload.get('target_ref') or ''}".strip()
                    )
                elif kind == "intervention" and current:
                    if payload.get("spoken") and payload.get("text"):
                        turns[current].spoken.append(payload["text"][:70])
                    else:
                        turns[current].suppressed.append(str(payload.get("outcome")))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}event stream ended: {type(exc).__name__}{RESET}")

    if not order:
        print(f"  {RED}No turns observed.{RESET} Nothing reached ingestion -- the "
              f"microphone, the relay or the transcript path is the break.\n")
        return 1

    for tid in order:
        print(turns[tid].render(agent_uid))

    # What the machine can conclude from what it saw.
    print(f"\n  {BOLD}Observed{RESET}")
    operator = [t for t in turns.values() if t.uid != agent_uid]
    echoes = [t for t in turns.values() if t.uid == agent_uid]
    stage("turns ingested", bool(operator), f"{len(operator)} from the operator")
    stage("claims extracted", any(t.claims for t in operator), "reasoning ran")
    stage("risk evaluated", any(t.verdicts for t in operator), "engine reached")
    spoke = any(t.spoken for t in operator)
    stage("aegis spoke", spoke, "governor produced audible output")

    counters = {}
    try:
        counters = httpx.get(f"{base_url}/api/metrics", timeout=10).json().get("counters", {})
    except Exception:  # noqa: BLE001
        pass
    echo_drops = counters.get("turns_self_echo", 0)
    if echoes:
        print(f"    {RED}{len(echoes)} turn(s) carried AEGIS's own uid and were "
              f"published as transcripts{RESET}")
    stage("self-echo rejected", echo_drops > 0 or not echoes,
          f"turns_self_echo={echo_drops}")

    print(f"\n  {YELLOW}Only you can confirm the last step:{RESET} "
          f"did you actually {BOLD}hear{RESET} AEGIS?\n")
    return 0 if spoke else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS live voice smoke test")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--watch", action="store_true", help="trace live turns after preflight")
    parser.add_argument("--seconds", type=int, default=90, help="how long to watch")
    args = parser.parse_args()

    if not preflight(args.url):
        print(f"\n  {RED}Preflight failed. Fix that before speaking into it.{RESET}\n")
        return 1
    print(f"\n  {GREEN}Preflight passed.{RESET} "
          f"{DIM}Everything provable without a microphone works.{RESET}")

    if not args.watch:
        print(f"  {DIM}Re-run with --watch, then join voice and speak.{RESET}\n")
        return 0
    return watch(args.url, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
