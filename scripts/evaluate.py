#!/usr/bin/env python3
"""
Evaluation harness.

Replays hand-authored adversarial scenarios through the real pipeline and
reports what happened. It is the difference between "we believe AEGIS catches
decision reversals" and "here is the run where it did, and the one where it
did not".

Two rules the numbers depend on:

* **Ground truth is authored before the run**, in
  ``data/transcripts/adversarial.json``, from what the system *should* do.
  Deriving expectations from current behaviour would make the evaluation
  circular and worth nothing.
* **Each scenario gets a fresh runtime.** State leaking between scenarios
  would let one scenario's stale hypothesis quietly change another's verdict,
  and the results would be unreproducible.

The set is small and hackathon-scoped, and is reported as such. An honest
twelve-scenario suite beats a claimed benchmark nobody ran.

Run:  python scripts/evaluate.py [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
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

SCENARIO_PATH = ROOT / "data" / "transcripts" / "adversarial.json"

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class ScenarioResult:
    scenario_id: str
    probes: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    extraction_degraded: bool = False


def _build_runtime(clock: ManualClock):
    base = load_config(env={}, dotenv_path=Path("/nonexistent"))
    config = AppConfig(
        agora=base.agora,
        llm=base.llm,
        governor=GovernorConfig(rate_limit_seconds=45.0),
        pipeline=base.pipeline,
        api=base.api,
        database_path=base.database_path,
        incident_id="evaluation",
        log_level="CRITICAL",
    )
    return build_runtime(config, clock=clock, sink=RecordingSink(clock=clock),
                         database_path=":memory:")


def run_scenario(scenario: dict) -> ScenarioResult:
    clock = ManualClock()
    runtime = _build_runtime(clock)
    result = ScenarioResult(scenario_id=scenario["id"], probes=scenario.get("probes", ""), passed=True)

    try:
        for metric, value in (scenario.get("setup", {}).get("telemetry") or {}).items():
            runtime.telemetry.set_value(metric, value)

        spoken: list[str] = []
        codes: set[str] = set()
        degraded = False

        for index, turn in enumerate(scenario["turns"], start=1):
            clock.advance(turn.get("advance_seconds", 5))
            # Reality is allowed to move mid-scenario. Without this the most
            # interesting failure mode -- a theory that was true when an
            # action was proposed and is not true any more -- cannot be
            # written down at all.
            for metric, value in (turn.get("set_telemetry") or {}).items():
                runtime.telemetry.set_value(metric, value)
            event = TranscriptEvent(
                uid=turn["uid"],
                turn_id=f"{scenario['id']}-{index}",
                role="human",
                text=turn["text"],
                final=True,
                timestamp=clock.now(),
            )
            turn_result = runtime.pipeline.handle_transcript(event)
            spoken.extend(turn_result.spoken)
            degraded = degraded or turn_result.degraded
            for verdict in turn_result.verdicts:
                codes.update(code.value for code in verdict.codes)

        result.spoken = spoken
        result.codes = sorted(codes)
        result.extraction_degraded = degraded
        result.failures = _check(scenario.get("expect", {}), runtime, clock, spoken, codes)
        result.passed = not result.failures
        return result
    finally:
        runtime.close()


def _check(expect: dict, runtime, clock, spoken: list[str], codes: set[str]) -> list[str]:
    problems: list[str] = []
    view = runtime.store.incident_view(captured_at=clock.now())

    if "intervenes" in expect:
        if expect["intervenes"] and not spoken:
            problems.append("expected an intervention, AEGIS stayed silent")
        if not expect["intervenes"] and spoken:
            problems.append(f"expected silence, AEGIS said {spoken[0]!r}")

    for wanted in expect.get("codes", ()):
        if wanted not in codes:
            problems.append(f"expected finding {wanted}, got {sorted(codes) or 'none'}")

    for unwanted in expect.get("not_codes", ()):
        if unwanted in codes:
            problems.append(f"finding {unwanted} fired but should not have")

    if "max_spoken" in expect and len(spoken) > expect["max_spoken"]:
        problems.append(
            f"spoke {len(spoken)} times, limit is {expect['max_spoken']} — rate limit breached"
        )

    if "pending_actions" in expect:
        pending = [a for a in view.proposed_actions if a.status.value == "pending"]
        if len(pending) != expect["pending_actions"]:
            problems.append(
                f"expected {expect['pending_actions']} pending action(s), found {len(pending)}"
            )

    if expect.get("no_action_confirmed"):
        confirmed = [a for a in view.proposed_actions if a.status.value == "confirmed"]
        if confirmed:
            problems.append(
                f"an action was treated as authorised without a clear human decision: "
                f"{confirmed[0].target_ref}"
            )

    if expect.get("action_not_confirmed_without_human"):
        for action in view.proposed_actions:
            if action.status.value == "confirmed" and not action.resolved_by_uid:
                problems.append("an action was confirmed with no human attributed to it")

    if "min_spoken" in expect and len(spoken) < expect["min_spoken"]:
        problems.append(
            f"expected at least {expect['min_spoken']} intervention(s), got {len(spoken)}"
        )

    for phrase in expect.get("spoken_contains", ()):
        if not any(phrase.lower() in line.lower() for line in spoken):
            problems.append(f"nothing AEGIS said mentioned {phrase!r}: {spoken}")

    for phrase in expect.get("spoken_excludes", ()):
        if any(phrase.lower() in line.lower() for line in spoken):
            problems.append(f"AEGIS mentioned {phrase!r} and should not have")

    if "spoken_order" in expect:
        # Which risk reached the room *first* is the whole question when two
        # compete for one channel, so it is asserted positionally.
        for position, phrase in enumerate(expect["spoken_order"]):
            if phrase is None:
                continue
            if position >= len(spoken):
                problems.append(f"expected an intervention at position {position} mentioning {phrase!r}")
            elif phrase.lower() not in spoken[position].lower():
                problems.append(
                    f"intervention {position} should mention {phrase!r}, said {spoken[position]!r}"
                )

    if "action_status" in expect:
        statuses = [a.status.value for a in view.proposed_actions]
        if expect["action_status"] not in statuses:
            problems.append(f"expected an action to be {expect['action_status']}, got {statuses}")

    if "resolved_by_uid" in expect:
        resolvers = [a.resolved_by_uid for a in view.proposed_actions if a.resolved_by_uid]
        if expect["resolved_by_uid"] not in resolvers:
            problems.append(
                f"expected the resolution to be attributed to {expect['resolved_by_uid']}, got {resolvers}"
            )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS evaluation harness")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    suite = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    results = [run_scenario(scenario) for scenario in suite["scenarios"]]
    passed = [r for r in results if r.passed]

    if args.json:
        print(json.dumps(
            {
                "suite": suite["name"],
                "total": len(results),
                "passed": len(passed),
                "results": [
                    {
                        "id": r.scenario_id,
                        "passed": r.passed,
                        "failures": r.failures,
                        "codes": r.codes,
                        "spoken_count": len(r.spoken),
                        "extraction_degraded": r.extraction_degraded,
                    }
                    for r in results
                ],
            },
            indent=2,
        ))
        return 0 if len(passed) == len(results) else 1

    colour = sys.stdout.isatty()

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if colour else text

    print()
    print(paint(f"  AEGIS evaluation — {suite['name']}", BOLD))
    print(paint(f"  {len(results)} hand-authored scenarios. Small and hackathon-scoped, stated as such.", DIM))
    print()

    for result in results:
        mark = paint("PASS", GREEN) if result.passed else paint("FAIL", RED)
        print(f"  [{mark}] {result.scenario_id}")
        print(paint(f"         probes: {result.probes}", DIM))
        if result.codes:
            print(paint(f"         findings: {', '.join(result.codes)}", DIM))
        for line in result.spoken:
            print(paint(f"         said: {line[:110]}", DIM))
        for failure in result.failures:
            print(paint(f"         ✗ {failure}", RED))
        print()

    summary = f"  {len(passed)}/{len(results)} scenarios passed"
    print(paint(summary, GREEN + BOLD if len(passed) == len(results) else RED + BOLD))
    print()
    return 0 if len(passed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
