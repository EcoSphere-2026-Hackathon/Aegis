#!/usr/bin/env python3
"""
Performance measurement.

Every performance claim this project makes should be reproducible by running
this file, not argued from the shape of the code. It measures three things
that are actually load-bearing:

* **Per-turn latency by stage.** AEGIS speaks into a live conversation, so
  what matters is the time from a final transcript to a decision, broken down
  far enough to say *which* stage owns it.
* **How read cost scales with incident length.** The naive design reads the
  whole incident to evaluate one action, which is O(incident) per turn and
  gets slower for exactly as long as the incident lasts. This compares that
  against the working-set read the pipeline actually uses.
* **What the extraction fast path and cache avoid.** Both exist to remove
  round trips from the critical path; the counters say how many.

The model provider is deterministic and local, so these numbers isolate the
system's own cost. A hosted LLM adds its own latency on top, which is why the
extraction stage is reported separately from everything else.

Run:  python scripts/benchmark.py [--turns 200] [--json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common.clock import ManualClock  # noqa: E402
from backend.common.config import AppConfig, GovernorConfig, load_config  # noqa: E402
from backend.common.logging import configure_logging  # noqa: E402
from backend.common.models import TranscriptEvent  # noqa: E402
from backend.pipeline.factory import build_runtime  # noqa: E402
from backend.pipeline.sinks import RecordingSink  # noqa: E402

configure_logging("CRITICAL")

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

#: A rotation that exercises every path: chatter, a grounded claim, an action
#: with a blast radius, a hold, a confirmation, and filler that should never
#: reach the provider.
UTTERANCES = [
    "Payments are throwing 500s, seeing timeouts.",
    "Pool utilization looks fine, like 40%.",
    "okay",
    "Let's rollback Core to the last version.",
    "Hold on, don't rollback yet.",
    "mm hmm",
    "Error rate is around 12%.",
    "Let's restart search-index.",
    "yeah",
    "It might be the retry storm.",
]


def _runtime(clock: ManualClock):
    base = load_config(env={}, dotenv_path=Path("/nonexistent"), project_root=None)
    config = AppConfig(
        agora=base.agora,
        llm=base.llm,
        governor=GovernorConfig(rate_limit_seconds=45.0),
        pipeline=base.pipeline,
        api=base.api,
        database_path=base.database_path,
        incident_id="benchmark",
        log_level="CRITICAL",
    )
    return build_runtime(config, clock=clock, sink=RecordingSink(clock=clock),
                         database_path=":memory:")


def measure_turns(turns: int) -> dict:
    """Drive `turns` utterances through the real pipeline and time each one."""
    clock = ManualClock()
    runtime = _runtime(clock)
    try:
        durations: list[float] = []
        for index in range(turns):
            clock.advance(5)
            event = TranscriptEvent(
                uid="100" + str(index % 3),
                turn_id=f"bench-{index}",
                role="human",
                text=UTTERANCES[index % len(UTTERANCES)],
                final=True,
                timestamp=clock.now(),
            )
            started = time.perf_counter()
            runtime.pipeline.handle_transcript(event)
            durations.append((time.perf_counter() - started) * 1000.0)

        snapshot = runtime.metrics.snapshot()
        return {
            "turns": turns,
            "wall_ms": {
                "p50": round(statistics.median(durations), 3),
                "p95": round(sorted(durations)[int(len(durations) * 0.95) - 1], 3),
                "max": round(max(durations), 3),
                "mean": round(statistics.fmean(durations), 3),
            },
            "stages": snapshot["stages"],
            "counters": snapshot["counters"],
            "extraction": {
                **runtime.metrics.derived(),
                "cache_entries": runtime.extraction.cache_size,
            },
            "scheduling": runtime.governor.scheduling_stats(),
            "final_incident_size": {
                "claims": len(runtime.store.timeline()),
                "state_version": runtime.store.version,
            },
        }
    finally:
        runtime.close()


def measure_read_scaling(sizes: tuple[int, ...]) -> list[dict]:
    """Full-incident read vs working-set read, as the incident grows.

    The risk engine needs one action, the hypothesis justifying it, and the
    decisions about that target. Reading the whole incident to get them is
    correct and gets steadily slower; this is the measurement that says by
    how much.
    """
    rows: list[dict] = []
    for size in sizes:
        clock = ManualClock()
        runtime = _runtime(clock)
        try:
            for index in range(size):
                clock.advance(5)
                runtime.pipeline.handle_transcript(
                    TranscriptEvent(
                        uid="100" + str(index % 3),
                        turn_id=f"scale-{index}",
                        role="human",
                        text=UTTERANCES[index % len(UTTERANCES)],
                        final=True,
                        timestamp=clock.now(),
                    )
                )

            # End on a proposal nothing answers, so there is always an
            # action to evaluate against. Relying on one surviving the
            # rotation is how this measurement silently produced no rows at
            # all when the conversation happened to resolve everything.
            clock.advance(5)
            runtime.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001",
                    turn_id=f"scale-{size}-final",
                    role="human",
                    text="Let's rollback Core to the last version.",
                    final=True,
                    timestamp=clock.now(),
                )
            )

            pending = runtime.store.pending_actions()
            if not pending:
                raise RuntimeError(
                    "read-scaling measurement has no pending action to evaluate; "
                    "the scenario changed and the numbers would be silently missing"
                )
            action = pending[0]
            now = clock.now()

            # Bound explicitly rather than captured: these are called inside
            # the loop, but a late-binding closure over a loop variable is a
            # bug waiting for the next person to move the call.
            store = runtime.store
            full = _time_call(partial(store.snapshot, captured_at=now))
            working = _time_call(partial(store.working_set_for, action, captured_at=now))
            all_evidence = _time_call(store.evidence)
            latest_evidence = _time_call(store.latest_evidence_per_metric)

            snapshot = runtime.store.snapshot(captured_at=now)
            working_set = runtime.store.working_set_for(action, captured_at=now)
            rows.append(
                {
                    "turns": size,
                    "full_snapshot_ms": full,
                    "working_set_ms": working,
                    "speedup": round(full / working, 2) if working else None,
                    "full_rows": len(snapshot.hypotheses)
                    + len(snapshot.decisions)
                    + len(snapshot.proposed_actions),
                    "working_rows": len(working_set.hypotheses)
                    + len(working_set.decisions)
                    + len(working_set.proposed_actions),
                    "all_evidence_ms": all_evidence,
                    "latest_evidence_ms": latest_evidence,
                    "all_evidence_rows": len(runtime.store.evidence()),
                    "latest_evidence_rows": len(runtime.store.latest_evidence_per_metric()),
                }
            )
        finally:
            runtime.close()
    return rows


def _time_call(call, *, repeats: int = 50) -> float:
    """Median of `repeats` calls, in milliseconds.

    Median rather than mean: one SQLite page fault should not decide the
    number a design conclusion rests on.
    """
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000.0)
    return round(statistics.median(samples), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS performance measurement")
    parser.add_argument("--turns", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    turns = measure_turns(args.turns)
    scaling = measure_read_scaling((20, 100, 400))

    if args.json:
        print(json.dumps({"turns": turns, "read_scaling": scaling}, indent=2))
        return 0

    print(f"\n  {BOLD}AEGIS benchmark{RESET}")
    print(f"  {DIM}deterministic provider, in-memory store; a hosted LLM adds its own latency{RESET}\n")

    wall = turns["wall_ms"]
    print(f"  {BOLD}Per-turn latency{RESET} over {turns['turns']} turns")
    print(f"    p50 {wall['p50']} ms · p95 {wall['p95']} ms · max {wall['max']} ms")
    print("\n  by stage:")
    for stage, values in sorted(turns["stages"].items()):
        print(
            f"    {stage:<16} n={values['count']:<5} "
            f"p50 {values['p50_ms']:>7} ms   p95 {values['p95_ms']:>7} ms"
        )

    extraction = turns["extraction"]
    print(f"\n  {BOLD}Provider round trips avoided{RESET}")
    print(
        f"    {extraction['provider_calls_avoided']} of {extraction['extraction_requests']} "
        f"requests ({extraction['avoidance_rate'] * 100:.0f}%) never reached the model"
    )
    print(
        f"    fast path {turns['counters'].get('extraction_fast_path_hits', 0)} · "
        f"cache {turns['counters'].get('extraction_cache_hits', 0)} · "
        f"entries held {extraction['cache_entries']}"
    )

    print(f"\n  {BOLD}Read cost as the incident grows{RESET}")
    print(f"    {'turns':>6} {'full':>10} {'working set':>13} {'speedup':>9}  rows")
    for row in scaling:
        print(
            f"    {row['turns']:>6} {row['full_snapshot_ms']:>9} ms "
            f"{row['working_set_ms']:>12} ms {str(row['speedup']) + 'x':>9}  "
            f"{row['full_rows']} -> {row['working_rows']}"
        )
    print(f"\n    {'turns':>6} {'all evidence':>13} {'latest/metric':>15} rows")
    for row in scaling:
        print(
            f"    {row['turns']:>6} {row['all_evidence_ms']:>12} ms "
            f"{row['latest_evidence_ms']:>14} ms  "
            f"{row['all_evidence_rows']} -> {row['latest_evidence_rows']}"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
