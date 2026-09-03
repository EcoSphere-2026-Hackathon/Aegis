#!/usr/bin/env python3
"""
Agora preflight, and the checklist for what preflight cannot answer.

Two jobs, deliberately kept in one file so the second is impossible to miss.

**Job one: fail early, on the ground.** Everything that can go wrong before a
demo and be discovered in five seconds instead of five minutes into a live
call. It checks the configuration, then -- with ``--live`` -- actually joins
the channel, speaks one line, and leaves. That is the earliest point at which
the credentials, the project id, the channel and the REST surface are all
proven together.

**Job two: state precisely what is still unverified.** Preflight proves the
requests are accepted. It cannot prove the *behaviour* the design rests on:
whether an INTERRUPT actually cuts through a human mid-sentence, whether
manual turn detection keeps the agent quiet, whether speaker attribution
survives two people talking at once. Those are answerable only by a person
with a headset, so this prints the checklist for that person rather than
letting the gap sit in someone's head.

Run:  python scripts/check_agora.py            # configuration only, no network
      python scripts/check_agora.py --live     # join, speak, leave (uses credits)
      python scripts/check_agora.py --checklist
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

from backend.agora.client import AgoraClient  # noqa: E402
from backend.common.config import load_config  # noqa: E402
from backend.common.errors import AegisError  # noqa: E402
from backend.common.logging import configure_logging  # noqa: E402

configure_logging("CRITICAL")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

OK = f"{GREEN}ok{RESET}"
MISSING = f"{RED}missing{RESET}"


#: What a person has to confirm with a real channel and a headset. Each line
#: names the assumption, how to test it, and what it breaks if it is wrong --
#: because "verify the voice path" is not an instruction anyone can act on
#: under demo pressure.
MANUAL_CHECKS = (
    (
        "The Customer ID / Secret pair exists at all",
        "Agora Console → Developer Toolkit → RESTful API. Not the App Certificate.",
        "Nothing REST works. This is the one credential that can block the demo "
        "outright, and it has never been confirmed to exist for this account.",
    ),
    (
        "INTERRUPT cuts through a human mid-sentence",
        "Have someone read a long sentence aloud; trigger an intervention halfway.",
        "AEGIS waits its turn, which for a system whose whole premise is "
        "interrupting a bad decision is the same as not working.",
    ),
    (
        "Manual turn detection keeps the agent silent",
        "Join, then hold a two-minute ordinary conversation. Count unsolicited replies.",
        "The agent chats. Fallback: bypass the agent's built-in LLM slot entirely "
        "and drive speech only through /speak.",
    ),
    (
        "Speaker UIDs are attributed correctly",
        "Two people on the bridge; confirm each transcript event carries the right uid.",
        "Decisions get attributed to the wrong human in the ledger -- the audit "
        "trail becomes wrong rather than merely incomplete.",
    ),
    (
        "Attribution survives overlapping speech",
        "Both speak simultaneously for ~5 seconds; inspect the transcript events.",
        "Same as above, in the situation where it is most likely to happen.",
    ),
    (
        "RTM transcripts arrive as final events with stable turn ids",
        "Watch /api/events while talking. Interim events must share the final one's id.",
        "Idempotency is keyed on turn id. Unstable ids mean duplicated claims; ids "
        "shared with interim events mean the final turn is dropped as a duplicate.",
    ),
    (
        "End-to-end latency from utterance to audible intervention",
        "Say the demo's beat-2 line; measure until AEGIS is audible. Target: 1-2s.",
        "The backend's own p95 is ~3.5 ms; everything else is ASR, the model and "
        "TTS. If this is slow, the backend is not where to look.",
    ),
    (
        "Rapid consecutive turns do not reorder",
        "Three short utterances back to back; confirm timeline order matches speech.",
        "The timeline is ordered by event timestamp, so a bad clock upstream shows "
        "up here rather than being corrected downstream.",
    ),
    (
        "The agent survives a mid-call reconnect",
        "Drop the network for ~10s during a call, restore, keep talking.",
        "The ingest queue is in memory. A restart loses what was queued; a "
        "reconnect without a restart should not.",
    ),
)


def check_config() -> int:
    config = load_config()
    agora = config.agora
    print(f"\n  {BOLD}Configuration{RESET}\n")

    rows = [
        ("AGORA_APP_ID", agora.app_id, bool(agora.app_id)),
        ("AGORA_CHANNEL_NAME", agora.channel_name, bool(agora.channel_name)),
        ("AGORA_CUSTOMER_ID", "set" if agora.customer_id.reveal() else "", bool(agora.customer_id.reveal())),
        ("AGORA_CUSTOMER_SECRET", "set" if agora.customer_secret.reveal() else "", bool(agora.customer_secret.reveal())),
        # The browser never receives the certificate -- it receives short-lived
        # tokens minted from it. Without it no participant can join a channel,
        # so a preflight that omits it passes on a configuration that cannot
        # actually carry a voice demo.
        ("AGORA_APP_CERTIFICATE", "set" if agora.app_certificate.reveal() else "",
         bool(agora.app_certificate.reveal())),
        ("AGORA_BASE_URL", agora.base_url, bool(agora.base_url)),
        ("agent uid", agora.agent_uid, bool(agora.agent_uid)),
        ("request timeout", f"{agora.request_timeout_seconds}s", True),
    ]
    for name, value, present in rows:
        status = OK if present else MISSING
        shown = value if present else "-"
        print(f"    {name:<24} {status:<20} {DIM}{shown}{RESET}")

    print(f"\n    LLM provider             {DIM}{config.llm.provider}{RESET}")
    if config.llm.provider == "deterministic":
        print(
            f"    {YELLOW}The offline extractor is in use. The demo runs, but the real "
            f"model path is not exercised.{RESET}"
        )

    if not agora.is_authenticated:
        print(
            f"\n  {RED}Not authenticated.{RESET} Set AGORA_CUSTOMER_ID and "
            f"AGORA_CUSTOMER_SECRET from Console → Developer Toolkit → RESTful API."
        )
        print(f"  {DIM}These are not the App ID and App Certificate; those sign RTC "
              f"tokens and will fail here with a 401.{RESET}")
        return 1

    if not agora.can_issue_client_tokens:
        # REST auth is only half the story now: the browser joins the channel
        # with a token minted from the App Certificate, so voice cannot start
        # without it even though every REST call would succeed.
        print(
            f"\n  {RED}Cannot issue browser tokens.{RESET} Set AGORA_APP_ID and "
            f"AGORA_APP_CERTIFICATE from Console → Project."
        )
        print(f"  {DIM}REST calls would work, but no participant could join the "
              f"voice channel.{RESET}")
        return 1

    print(f"\n  {GREEN}Configuration is complete.{RESET} "
          f"{DIM}Run with --live to prove the credentials actually work.{RESET}")
    return 0


def check_live() -> int:
    """Join, speak, leave. The earliest honest proof that REST works."""
    config = load_config()
    if not config.agora.is_authenticated:
        print(f"\n  {RED}Cannot run live checks without credentials.{RESET}")
        return 1

    print(f"\n  {BOLD}Live check{RESET}  {DIM}joins {config.agora.channel_name}, "
          f"says one line, leaves{RESET}\n")
    agent_id = None
    client = AgoraClient(config.agora)
    try:
        agent_id = client.join()
        print(f"    join   {OK}  {DIM}agent {agent_id}{RESET}")
    except AegisError as exc:
        print(f"    join   {RED}failed{RESET}  {exc}")
        print(f"    {DIM}context: {dict(exc.context)}{RESET}")
        return 1

    try:
        client.speak(agent_id, "AEGIS preflight. If you can hear this, the voice path works.")
        print(f"    speak  {OK}")
    except AegisError as exc:
        print(f"    speak  {RED}failed{RESET}  {exc}")
        return 1
    finally:
        try:
            client.leave(agent_id)
            print(f"    leave  {OK}")
        except AegisError as exc:
            print(f"    leave  {YELLOW}failed{RESET}  {exc}  "
                  f"{DIM}(the agent may linger until idle_timeout){RESET}")
        client.close()

    print(f"\n  {GREEN}REST works.{RESET} That proves the requests are accepted. "
          f"It does not prove the behaviour below.")
    return 0


def print_checklist() -> int:
    print(f"\n  {BOLD}What preflight cannot answer{RESET}")
    print(f"  {DIM}Needs a real channel and a person with a headset. Nothing below is "
          f"verified by any automated test in this repository.{RESET}\n")
    for index, (assumption, how, breaks) in enumerate(MANUAL_CHECKS, start=1):
        print(f"    {index}. {BOLD}{assumption}{RESET}")
        print(f"       {DIM}test:{RESET}   {how}")
        print(f"       {DIM}breaks:{RESET} {breaks}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS Agora preflight")
    parser.add_argument("--live", action="store_true",
                        help="join the channel, speak once and leave (uses Agora credits)")
    parser.add_argument("--checklist", action="store_true",
                        help="print only the manual verification checklist")
    args = parser.parse_args()

    if args.checklist:
        return print_checklist()

    code = check_config()
    if args.live and code == 0:
        code = check_live()
    print_checklist()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
