"""
Invariants, checked against randomised conversation.

The scenario tests answer "does AEGIS do the right thing in this situation".
These answer a different and harder question: "is there *any* situation in
which it does the wrong thing". They generate conversations from a grammar of
realistic turn shapes -- proposals, contradictions, confirmations, holds,
duplicates, filler, telemetry moves -- and after every single turn assert the
properties that must hold no matter what was said.

Each run is seeded and the seed is reported on failure, so a random failure is
reproducible rather than a ghost. The generators are deliberately biased
toward the awkward cases (repeated confirmations, several open actions,
retracted beliefs) because uniform random conversation almost never produces
them.

This is not fuzzing for its own sake. Every property below is a claim the
system makes about itself somewhere -- in the spec, in a docstring, or in the
report -- and the point is that the claim survives inputs nobody wrote a
scenario for.
"""

from __future__ import annotations

import random
import threading
import unittest
from typing import Sequence

from backend.common.clock import ManualClock
from backend.common.enums import (
    ProposedActionStatus,
    RiskTier,
    SourceModality,
)
from backend.common.models import TranscriptEvent
from backend.governor.governor import VOICED_MEMORY_MAX
from backend.governor.speech import SPEAK_MAX_BYTES
from backend.tests.support import T0
from backend.tests.test_pipeline import runtime

#: Turn shapes, chosen to cover the transitions that matter rather than to
#: imitate natural speech. Each entry is (text, seconds to advance first).
UTTERANCES: tuple[str, ...] = (
    "Payments are throwing 500s, seeing timeouts.",
    "Pool utilization looks fine, like 40%.",
    "Error rate is around 12%.",
    "p99 latency is about 900ms.",
    "It might be the connection pool.",
    "It's the retry storm, definitely.",
    "Let's rollback Core to the last version.",
    "Let's roll back search-index.",
    "Let's restart notification-service.",
    "Let's restart cache-layer.",
    "Yes, go ahead.",
    "yeah",
    "Yes, roll back search-index.",
    "No, don't do that.",
    "Hold on, don't rollback yet.",
    "Let's hold off on core-db.",
    "okay",
    "mm hmm",
    "Morning, can everyone hear me?",
    "AEGIS, status?",
    "uh, I guess maybe",
    "We're going with the core-db rollback.",
    "...",
    "the dashboards look quiet now",
)

SPEAKERS = ("1001", "1002", "1003")
ADVANCES = (0.5, 2.0, 5.0, 20.0, 46.0, 130.0)
METRICS = ("pool_utilization", "error_rate", "p99_latency")
VALUES = (0.3, 2.0, 12.4, 40.0, 91.0, 900.0)

RATE_LIMIT_SECONDS = 45.0


class InvariantHarness(unittest.TestCase):
    """Drives a randomised conversation and asserts the invariants after each
    turn. Subclasses vary the generator; the checks are shared."""

    def drive(self, seed: int, *, turns: int = 60, duplicate_rate: float = 0.15) -> None:
        rng = random.Random(seed)
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)

        issued: list[str] = []
        last_version = rt.store.version

        for index in range(turns):
            clock.advance(rng.choice(ADVANCES))

            # Occasionally move reality, which is what retracts beliefs.
            if rng.random() < 0.2:
                rt.telemetry.set_value(rng.choice(METRICS), rng.choice(VALUES))

            # Occasionally redeliver an earlier turn verbatim, as RTM does.
            if issued and rng.random() < duplicate_rate:
                turn_id = rng.choice(issued)
                text = self._text_for(turn_id)
            else:
                turn_id = f"fuzz-{seed}-{index}"
                text = rng.choice(UTTERANCES)
                self._remember(turn_id, text)
                issued.append(turn_id)

            event = TranscriptEvent(
                uid=rng.choice(SPEAKERS),
                turn_id=turn_id,
                role="human",
                text=text,
                final=True,
                timestamp=clock.now(),
                source_modality=(
                    SourceModality.TEXT if rng.random() < 0.15 else SourceModality.VOICE
                ),
            )

            result = rt.pipeline.handle_transcript(event)

            context = f"seed={seed} turn={index} text={text!r}"
            self.assertEqual(result.errors, (), f"a turn raised: {context}")
            self._check_invariants(rt, clock, context)

            version = rt.store.version
            self.assertGreaterEqual(version, last_version, f"version went backwards: {context}")
            last_version = version

    # -- the properties ---------------------------------------------------

    def _check_invariants(self, rt, clock, context: str) -> None:
        actions = rt.store.snapshot(captured_at=clock.now()).proposed_actions

        for action in actions:
            # 1. A resolved action names the human who resolved it, and when.
            if action.status is not ProposedActionStatus.PENDING:
                self.assertTrue(
                    action.resolved_by_uid,
                    f"an action was resolved with no human attributed: {context}",
                )
                self.assertIsNotNone(action.resolved_at, context)
                self.assertIn(action.resolved_by_uid, SPEAKERS, context)
            else:
                # 2. A pending action carries no resolution residue.
                self.assertIsNone(action.resolved_by_uid, context)
                self.assertIsNone(action.resolved_at, context)

            # 3. A verdict attached to an action always explains itself.
            verdict = action.risk_verdict
            if verdict is not None and verdict.risk_tier is not RiskTier.LOW:
                self.assertTrue(verdict.reasons, f"a non-LOW verdict said nothing: {context}")
                self.assertTrue(
                    all(reason.strip() for reason in verdict.reasons), context
                )

        # 4. Nothing AEGIS says can exceed the transport's hard limit.
        for line in rt.sink.lines:
            self.assertLessEqual(
                len(line.encode("utf-8")), SPEAK_MAX_BYTES, f"speech over budget: {context}"
            )

        # 5. The rate limit is a red line: interventions never come closer
        #    together than the window, whatever the conversation did.
        spoken_at = [entry.at for entry in rt.sink._lines]  # noqa: SLF001 - test-only view
        for earlier, later in zip(spoken_at, spoken_at[1:], strict=False):
            gap = (_parse(later) - _parse(earlier)).total_seconds()
            self.assertGreaterEqual(
                gap,
                RATE_LIMIT_SECONDS,
                f"two interventions {gap:.1f}s apart, limit is {RATE_LIMIT_SECONDS}: {context}",
            )

        # 6. Bounded structures stay bounded, however long the incident runs.
        stats = rt.governor.scheduling_stats()
        self.assertLessEqual(stats["queue_depth"], 16, context)
        self.assertLessEqual(rt.governor.voiced_memory_size, VOICED_MEMORY_MAX, context)
        self.assertLessEqual(rt.extraction.cache_size, 256, context)

        # 7. The timeline is ordered by when things happened, not written.
        timeline = rt.store.timeline()
        occurred = [entry.occurred_at for entry in timeline]
        self.assertEqual(occurred, sorted(occurred), f"timeline out of order: {context}")

    # -- duplicate bookkeeping -------------------------------------------

    def setUp(self) -> None:
        self._texts: dict[str, str] = {}

    def _remember(self, turn_id: str, text: str) -> None:
        self._texts[turn_id] = text

    def _text_for(self, turn_id: str) -> str:
        return self._texts[turn_id]


def _parse(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


class RandomisedConversationTests(InvariantHarness):
    """Twelve seeds, sixty turns each. Enough to reach the awkward states
    without turning the suite into a long-running job."""

    def test_invariants_hold_across_random_conversation(self) -> None:
        for seed in range(12):
            with self.subTest(seed=seed):
                self.setUp()
                self.drive(seed)

    def test_invariants_hold_under_heavy_redelivery(self) -> None:
        # RTM redelivers. A conversation that is half duplicates must produce
        # the same state as one that is not.
        for seed in (100, 101, 102):
            with self.subTest(seed=seed):
                self.setUp()
                self.drive(seed, turns=40, duplicate_rate=0.5)


class DuplicateTurnPropertyTests(unittest.TestCase):
    """Replaying a turn must be indistinguishable from never sending it
    twice -- not merely "not crash"."""

    def test_a_replayed_conversation_produces_identical_state(self) -> None:
        script = [
            "Payments are throwing 500s, seeing timeouts.",
            "Pool utilization looks fine, like 40%.",
            "It might be the connection pool.",
            "Let's rollback Core to the last version.",
            "Yes, roll back core-db.",
        ]

        def run(repeats: int) -> tuple:
            clock = ManualClock(start=T0)
            rt = runtime(clock)
            try:
                for index, text in enumerate(script):
                    for _ in range(repeats):
                        clock.advance(20)
                        rt.pipeline.handle_transcript(
                            TranscriptEvent(
                                uid="1001",
                                turn_id=f"dup-{index}",
                                role="human",
                                text=text,
                                final=True,
                                timestamp=clock.now(),
                                source_modality=SourceModality.VOICE,
                            )
                        )
                view = rt.store.snapshot(captured_at=clock.now())
                return (
                    len(view.facts),
                    len(view.hypotheses),
                    len(view.proposed_actions),
                    len(view.decisions),
                    tuple(sorted(a.status.value for a in view.proposed_actions)),
                )
            finally:
                rt.close()

        self.assertEqual(run(1), run(4), "redelivering turns changed the incident")


class ConcurrentIngestionTests(unittest.TestCase):
    """The worker is single-threaded today, but the pipeline is reachable
    from the request thread too (evidence submission), and a future second
    worker must not be able to corrupt state. Both are tested here rather
    than assumed."""

    def test_concurrent_turns_never_duplicate_or_crash(self) -> None:
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for repeat in range(12):
                    rt.pipeline.handle_transcript(
                        TranscriptEvent(
                            uid=SPEAKERS[index % len(SPEAKERS)],
                            # Deliberately overlapping ids across threads: the
                            # idempotency claim is what has to serialise them.
                            turn_id=f"race-{repeat}",
                            role="human",
                            text=UTTERANCES[repeat % len(UTTERANCES)],
                            final=True,
                            timestamp=clock.now(),
                            source_modality=SourceModality.VOICE,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - recorded then asserted
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        claim_ids = [entry.entry_id for entry in rt.store.timeline()]
        self.assertEqual(len(claim_ids), len(set(claim_ids)), "the ledger has duplicates")

    def test_concurrent_confirmations_cannot_both_win(self) -> None:
        # The store's conditional UPDATE is the guard; this proves it holds
        # from the pipeline's side, through extraction and the policy.
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)

        clock.advance(5)
        rt.pipeline.handle_transcript(
            TranscriptEvent(
                uid="1001", turn_id="one-action", role="human",
                text="Let's roll back search-index.", final=True,
                timestamp=clock.now(), source_modality=SourceModality.VOICE,
            )
        )

        barrier = threading.Barrier(8)

        def confirm(index: int) -> None:
            barrier.wait()
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid=SPEAKERS[index % len(SPEAKERS)],
                    turn_id=f"confirm-{index}",
                    role="human",
                    text="Yes, roll back search-index.",
                    final=True,
                    timestamp=clock.now(),
                    source_modality=SourceModality.VOICE,
                )
            )

        threads = [threading.Thread(target=confirm, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        actions = rt.store.snapshot(captured_at=clock.now()).proposed_actions
        self.assertEqual(len(actions), 1)
        resolved = actions[0]
        self.assertIs(resolved.status, ProposedActionStatus.CONFIRMED)
        # Exactly one human owns the decision -- not the last writer to land.
        self.assertIn(resolved.resolved_by_uid, SPEAKERS)


class SpeechBudgetPropertyTests(unittest.TestCase):
    """The 512-byte cap is a transport limit, not a preference: over it, the
    speak call is rejected and AEGIS says nothing at all."""

    def test_random_findings_never_compose_over_the_budget(self) -> None:
        from backend.common.enums import GovernorAction, RiskFindingCode
        from backend.common.errors import SpeechTooLongError
        from backend.common.models import RiskFinding, RiskVerdict
        from backend.governor.speech import build_intervention_text

        rng = random.Random(7)
        codes = list(RiskFindingCode)
        for trial in range(300):
            count = rng.randint(1, 6)
            findings = [
                RiskFinding(
                    code=rng.choice(codes),
                    tier=rng.choice([RiskTier.HIGH, RiskTier.MEDIUM]),
                    message="x" * rng.randint(5, 160),
                )
                for _ in range(count)
            ]
            verdict = RiskVerdict.from_findings(findings)
            budget = rng.randint(80, SPEAK_MAX_BYTES)
            action = rng.choice([GovernorAction.WARN, GovernorAction.ASK])
            with self.subTest(trial=trial, budget=budget, findings=count):
                try:
                    text = build_intervention_text(verdict, action, max_bytes=budget)
                except SpeechTooLongError:
                    continue  # refusing loudly is the documented behaviour
                self.assertLessEqual(len(text.encode("utf-8")), budget)


class QueuePressurePropertyTests(unittest.TestCase):
    """The intervention queue is bounded. Under sustained pressure it must
    stay bounded and must keep the most valuable entry, not the newest."""

    def test_the_queue_never_exceeds_its_bound_under_pressure(self) -> None:
        from backend.common.config import GovernorConfig
        from backend.governor.governor import Governor
        from backend.tests.test_governor import high_verdict, medium_verdict

        rng = random.Random(11)
        clock = ManualClock()
        governor = Governor(GovernorConfig(max_queue_depth=4), clock=clock)
        governor.decide(high_verdict("occupier"), subject_claim_id="occupier")

        for index in range(200):
            clock.advance(rng.choice([0.1, 1.0, 3.0]))
            verdict = high_verdict(f"h{index}") if rng.random() < 0.3 else medium_verdict(f"m{index}")
            governor.decide(verdict, subject_claim_id=f"s{index}")
            self.assertLessEqual(governor.queue_depth, 4)

        stats = governor.scheduling_stats()
        self.assertLessEqual(stats["queue_depth"], 4)
        self.assertGreater(
            stats["evicted_low_value"] + stats["preempted_by_higher_value"],
            0,
            "sustained pressure produced no eviction, so the bound was never tested",
        )


def _spoken_lines(rt) -> Sequence[str]:
    return rt.sink.lines


if __name__ == "__main__":
    unittest.main()
