#!/usr/bin/env python3
"""
Golden demo replay.

Runs the rehearsed script through the real pipeline with no audio and no
network, printing what AEGIS heard, what it concluded, and what it said. Two
jobs:

* **A regression gate.** Every turn carries expectations, and the script
  exits non-zero if any of them fail. "The demo still works" becomes a
  command anyone can run rather than a thing someone remembers to check.
* **A rehearsal aid.** Reading the transcript with the interventions in place
  is how you notice that a line lands badly, or that AEGIS says three things
  where two would land harder.

Run:  python scripts/run_golden_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

from backend.common.clock import ManualClock  # noqa: E402
from backend.common.config import AppConfig, GovernorConfig, load_config  # noqa: E402
from backend.common.logging import configure_logging  # noqa: E402
from backend.common.models import TranscriptEvent  # noqa: E402
from backend.pipeline.factory import build_runtime  # noqa: E402
from backend.pipeline.sinks import RecordingSink  # noqa: E402

# ``log_level`` on the config object only takes effect when an entry point
# applies it. These harnesses print a curated transcript; a stray warning in
# the middle of it is noise, not signal.
configure_logging("CRITICAL")

SCRIPT_PATH = ROOT / "data" / "transcripts" / "golden_demo.json"

DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _colour(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def run_golden_demo(*, verbose: bool = True) -> bool:
    colour = sys.stdout.isatty()
    script = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
    participants = script.get("participants", {})

    base = load_config(env={}, dotenv_path=Path("/nonexistent"))
    config = AppConfig(
        agora=base.agora,
        llm=base.llm,
        governor=GovernorConfig(rate_limit_seconds=45.0),
        pipeline=base.pipeline,
        api=base.api,
        database_path=base.database_path,
        incident_id="golden-demo",
        log_level="CRITICAL",
    )

    clock = ManualClock()
    runtime = build_runtime(config, clock=clock, sink=RecordingSink(clock=clock),
                            database_path=":memory:")

    failures: list[str] = []

    try:
        if verbose:
            print()
            print(_colour("  AEGIS — golden demo replay", BOLD, colour))
            print(_colour(f"  {script['description']}", DIM, colour))
            print()

        for index, turn in enumerate(script["turns"], start=1):
            clock.advance(turn.get("advance_seconds", 5))
            event = TranscriptEvent(
                uid=turn["uid"],
                turn_id=f"golden-{index}",
                role="human",
                text=turn["text"],
                final=True,
                timestamp=clock.now(),
            )
            result = runtime.pipeline.handle_transcript(event)
            speaker = participants.get(turn["uid"], f"uid {turn['uid']}")

            if verbose:
                print(f"  {_colour(speaker + ':', BOLD, colour)} {turn['text']}")
                for claim in result.claims:
                    if claim.type.value == "none":
                        continue
                    detail = []
                    if claim.target_ref:
                        detail.append(f"target={claim.target_ref}")
                    if claim.metric_ref:
                        value = f"={claim.claimed_value}{claim.claimed_unit or ''}" if claim.claimed_value is not None else ""
                        detail.append(f"metric={claim.metric_ref}{value}")
                    if claim.action_kind:
                        detail.append(f"kind={claim.action_kind.value}")
                    suffix = ("  " + " ".join(detail)) if detail else ""
                    print(_colour(f"      → {claim.type.value}{suffix}", DIM, colour))
                for line in result.spoken:
                    print(_colour(f"      ★ AEGIS: {line}", CYAN, colour))

            failures.extend(_check(turn, result, runtime, index))
            if verbose:
                print()

        if verbose:
            _print_final_state(runtime, clock, colour)

        if failures:
            print(_colour("  FAILED", RED + BOLD, colour))
            for failure in failures:
                print(_colour(f"    ✗ {failure}", RED, colour))
            print()
            return False

        print(_colour("  All golden-demo expectations met.", GREEN + BOLD, colour))
        print()
        return True
    finally:
        runtime.close()


def _check(turn: dict, result, runtime, index: int) -> list[str]:
    expect = turn.get("expect") or {}
    beat = turn.get("beat", index)
    problems: list[str] = []

    if "claim_types" in expect:
        actual = [claim.type.value for claim in result.claims if claim.type.value != "none"]
        for wanted in expect["claim_types"]:
            if wanted not in actual:
                problems.append(f"beat {beat}: expected a {wanted} claim, got {actual or 'none'}")

    if "intervenes" in expect:
        if expect["intervenes"] and not result.spoken:
            problems.append(f"beat {beat}: expected AEGIS to speak, it stayed silent")
        if not expect["intervenes"] and result.spoken:
            problems.append(f"beat {beat}: expected silence, AEGIS said {result.spoken[0]!r}")

    if "risk_tier" in expect:
        tiers = [verdict.risk_tier.value for verdict in result.verdicts]
        if expect["risk_tier"] not in tiers:
            problems.append(f"beat {beat}: expected a {expect['risk_tier']} verdict, got {tiers or 'none'}")

    if "codes" in expect:
        produced = {code.value for verdict in result.verdicts for code in verdict.codes}
        for wanted in expect["codes"]:
            if wanted not in produced:
                problems.append(f"beat {beat}: expected finding {wanted}, got {sorted(produced) or 'none'}")

    for fragment in expect.get("contains", ()):
        if not any(fragment.lower() in line.lower() for line in result.spoken):
            problems.append(f"beat {beat}: expected {fragment!r} in what AEGIS said")

    if expect.get("resolves_action"):
        if not result.resolved_action_ids:
            problems.append(f"beat {beat}: expected a human resolution to land, none did")
        else:
            wanted = expect.get("resolution_status")
            action = runtime.store.get_proposed_action(result.resolved_action_ids[0])
            if wanted and action.status.value != wanted:
                problems.append(
                    f"beat {beat}: expected the action to be {wanted}, it is {action.status.value}"
                )

    return problems


def _print_final_state(runtime, clock, colour: bool) -> None:
    view = runtime.store.incident_view(captured_at=clock.now())
    print(_colour("  ── final incident state ─────────────────────────────", DIM, colour))
    print(
        f"     {len(view.facts)} facts · {len(view.hypotheses)} theories · "
        f"{len(view.decisions)} decisions · {len(view.proposed_actions)} actions · "
        f"{len(view.evidence)} evidence · {len(view.interventions)} interventions"
    )
    for action in view.proposed_actions:
        tier = action.risk_verdict.risk_tier.value if action.risk_verdict else "—"
        who = action.resolved_by_uid or "nobody"
        print(f"     action {action.action_kind.value} {action.target_ref}: "
              f"{action.status.value} (risk {tier}, resolved by {who})")
    unresolved = [a for a in view.proposed_actions if a.status.value == "pending"]
    if unresolved:
        print(_colour(f"     {len(unresolved)} action(s) still awaiting a human decision", YELLOW, colour))
    print()


if __name__ == "__main__":
    raise SystemExit(0 if run_golden_demo() else 1)
