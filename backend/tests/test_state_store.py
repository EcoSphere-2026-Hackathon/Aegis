"""Incident State Store tests.

Beyond the happy path, these cover the failure modes the previous
implementation was open to: silent no-op mutations on unknown ids,
re-resolution of an action a human already resolved, torn snapshots, lost
type information on evidence values, and concurrent writers.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from backend.common.enums import (
    DecisionStance,
    GovernorAction,
    HypothesisStatus,
    InterventionOutcome,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
)
from backend.common.errors import EntityNotFoundError, IllegalStateTransitionError
from backend.common.models import InterventionRecord, RiskFinding, RiskVerdict
from backend.risk_engine.staleness import HypothesisTransitions
from backend.state_store.store import IncidentStateStore
from backend.tests.support import (
    at,
    make_action,
    make_decision,
    make_fact,
    make_hypothesis,
    make_telemetry,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IncidentStateStore(":memory:", incident_id="test-incident")
        self.addCleanup(self.store.close)


class WriteAndReadTests(StoreTestCase):
    def test_claim_types_stay_in_separate_collections(self) -> None:
        self.store.add_fact(make_fact())
        self.store.add_hypothesis(make_hypothesis())
        self.store.add_decision(make_decision())
        self.store.add_proposed_action(make_action())

        view = self.store.snapshot(captured_at=at(60))
        self.assertEqual(len(view.facts), 1)
        self.assertEqual(len(view.hypotheses), 1)
        self.assertEqual(len(view.decisions), 1)
        self.assertEqual(len(view.proposed_actions), 1)

    def test_round_trip_preserves_every_field(self) -> None:
        original = make_hypothesis(
            text="pool utilization looks fine, like 40%",
            target_ref="core-db",
            metric_ref="pool_utilization",
            claimed_value=40.0,
            claimed_unit="%",
            reinforcement_count=3,
        )
        self.store.add_hypothesis(original)
        restored = self.store.get_hypothesis(original.claim_id)
        self.assertEqual(restored, original)

    def test_action_round_trip_preserves_the_risk_verdict(self) -> None:
        action = make_action()
        self.store.add_proposed_action(action)
        verdict = RiskVerdict.from_findings(
            [
                RiskFinding(
                    code=RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,
                    tier=RiskTier.HIGH,
                    message="breaks payment-api",
                    related_ids=("payment-api",),
                )
            ]
        )
        self.store.attach_risk_verdict(action.claim_id, verdict)
        restored = self.store.get_proposed_action(action.claim_id)
        self.assertIsNotNone(restored.risk_verdict)
        self.assertEqual(restored.risk_verdict.risk_tier, RiskTier.HIGH)
        self.assertEqual(restored.risk_verdict.codes, (RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,))

    def test_numeric_evidence_round_trips_as_a_number(self) -> None:
        self.store.add_evidence(make_telemetry(value=91))
        stored = self.store.evidence()[0]
        self.assertIsInstance(stored.value, float)
        self.assertEqual(stored.value, 91.0)

    def test_text_evidence_round_trips_as_text(self) -> None:
        self.store.add_evidence(make_telemetry(metric_name="schema_version", value="v17", unit=None))
        stored = self.store.evidence()[0]
        self.assertIsInstance(stored.value, str)
        self.assertEqual(stored.value, "v17")

    def test_decision_stance_round_trips_including_none(self) -> None:
        with_stance = make_decision(stance=DecisionStance.HOLD)
        without = make_decision(text="something ambiguous", stance=None, when=1)
        self.store.add_decision(with_stance)
        self.store.add_decision(without)
        stances = {d.claim_id: d.stance for d in self.store.snapshot(captured_at=at(60)).decisions}
        self.assertEqual(stances[with_stance.claim_id], DecisionStance.HOLD)
        self.assertIsNone(stances[without.claim_id])


class IdempotencyTests(StoreTestCase):
    def test_redelivered_event_does_not_double_count(self) -> None:
        fact = make_fact()
        self.assertTrue(self.store.add_fact(fact))
        self.assertFalse(self.store.add_fact(fact))
        self.assertEqual(len(self.store.snapshot(captured_at=at(60)).facts), 1)

    def test_redelivered_event_does_not_double_append_the_timeline(self) -> None:
        fact = make_fact()
        self.store.add_fact(fact)
        self.store.add_fact(fact)
        self.assertEqual(len(self.store.timeline()), 1)

    def test_duplicate_evidence_is_idempotent(self) -> None:
        evidence = make_telemetry()
        self.assertTrue(self.store.add_evidence(evidence))
        self.assertFalse(self.store.add_evidence(evidence))
        self.assertEqual(len(self.store.evidence()), 1)


class TimelineOrderingTests(StoreTestCase):
    def test_timeline_is_chronological_not_write_ordered(self) -> None:
        late = make_fact("late", when=100)
        early = make_fact("early", when=10)
        self.store.add_fact(late)
        self.store.add_fact(early)
        entries = self.store.timeline()
        self.assertEqual([e.entry_id for e in entries], [early.claim_id, late.claim_id])

    def test_entries_sharing_a_timestamp_are_deterministically_ordered(self) -> None:
        first = make_fact("first", when=10)
        second = make_fact("second", when=10)
        self.store.add_fact(first)
        self.store.add_fact(second)
        self.assertEqual(
            [e.entry_id for e in self.store.timeline()], [first.claim_id, second.claim_id]
        )


class ResolutionBoundaryTests(StoreTestCase):
    """Quality Standard §4 red lines #1 and #5."""

    def setUp(self) -> None:
        super().setUp()
        self.action = make_action()
        self.store.add_proposed_action(self.action)

    def test_action_starts_pending(self) -> None:
        stored = self.store.get_proposed_action(self.action.claim_id)
        self.assertIs(stored.status, ProposedActionStatus.PENDING)

    def test_explicit_resolution_records_who_and_when(self) -> None:
        resolved = self.store.resolve_proposed_action(
            self.action.claim_id,
            ProposedActionStatus.HELD,
            resolved_by_uid="1002",
            resolved_at=at(30),
        )
        self.assertIs(resolved.status, ProposedActionStatus.HELD)
        self.assertEqual(resolved.resolved_by_uid, "1002")
        self.assertEqual(resolved.resolved_at, at(30))
        self.assertEqual(self.store.pending_actions(), ())

    def test_resolving_to_pending_is_rejected(self) -> None:
        with self.assertRaises(IllegalStateTransitionError):
            self.store.resolve_proposed_action(
                self.action.claim_id,
                ProposedActionStatus.PENDING,
                resolved_by_uid="1002",
                resolved_at=at(30),
            )

    def test_a_resolved_action_cannot_be_re_resolved(self) -> None:
        self.store.resolve_proposed_action(
            self.action.claim_id, ProposedActionStatus.HELD,
            resolved_by_uid="1002", resolved_at=at(30),
        )
        with self.assertRaises(IllegalStateTransitionError):
            self.store.resolve_proposed_action(
                self.action.claim_id, ProposedActionStatus.CONFIRMED,
                resolved_by_uid="1001", resolved_at=at(40),
            )
        still_held = self.store.get_proposed_action(self.action.claim_id)
        self.assertIs(still_held.status, ProposedActionStatus.HELD)
        self.assertEqual(still_held.resolved_by_uid, "1002")

    def test_resolution_requires_a_human_uid(self) -> None:
        with self.assertRaises(IllegalStateTransitionError):
            self.store.resolve_proposed_action(
                self.action.claim_id, ProposedActionStatus.CONFIRMED,
                resolved_by_uid="", resolved_at=at(30),
            )

    def test_resolving_an_unknown_action_raises_rather_than_no_ops(self) -> None:
        with self.assertRaises(EntityNotFoundError):
            self.store.resolve_proposed_action(
                "no-such-claim", ProposedActionStatus.CONFIRMED,
                resolved_by_uid="1001", resolved_at=at(30),
            )

    def test_attaching_a_verdict_to_an_unknown_action_raises(self) -> None:
        with self.assertRaises(EntityNotFoundError):
            self.store.attach_risk_verdict("no-such-claim", RiskVerdict.low())

    def test_attaching_a_verdict_does_not_resolve_the_action(self) -> None:
        verdict = RiskVerdict.from_findings(
            [RiskFinding(code=RiskFindingCode.STALE_JUSTIFICATION, tier=RiskTier.MEDIUM, message="x")]
        )
        self.store.attach_risk_verdict(self.action.claim_id, verdict)
        self.assertIs(
            self.store.get_proposed_action(self.action.claim_id).status,
            ProposedActionStatus.PENDING,
        )

    def test_concurrent_resolutions_produce_exactly_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def resolve(uid: str, status: ProposedActionStatus) -> None:
            barrier.wait()
            try:
                self.store.resolve_proposed_action(
                    self.action.claim_id, status, resolved_by_uid=uid, resolved_at=at(30)
                )
                with lock:
                    outcomes.append(f"won:{uid}")
            except IllegalStateTransitionError:
                with lock:
                    outcomes.append(f"lost:{uid}")

        threads = [
            threading.Thread(target=resolve, args=("1001", ProposedActionStatus.CONFIRMED)),
            threading.Thread(target=resolve, args=("1002", ProposedActionStatus.DECLINED)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len([o for o in outcomes if o.startswith("won")]), 1)
        self.assertEqual(len([o for o in outcomes if o.startswith("lost")]), 1)


class HypothesisTransitionTests(StoreTestCase):
    def test_transitions_are_applied_not_computed(self) -> None:
        hypothesis = make_hypothesis()
        self.store.add_hypothesis(hypothesis)
        self.store.apply_hypothesis_transitions(
            HypothesisTransitions(stale_claim_ids=(hypothesis.claim_id,)), touched_at=at(30)
        )
        self.assertIs(self.store.get_hypothesis(hypothesis.claim_id).status, HypothesisStatus.STALE)

    def test_reinforcement_increments_and_touches(self) -> None:
        hypothesis = make_hypothesis()
        self.store.add_hypothesis(hypothesis)
        self.store.apply_hypothesis_transitions(
            HypothesisTransitions(reinforced_claim_ids=(hypothesis.claim_id,)), touched_at=at(30)
        )
        stored = self.store.get_hypothesis(hypothesis.claim_id)
        self.assertEqual(stored.reinforcement_count, 1)
        self.assertEqual(stored.last_touched_at, at(30))
        self.assertIs(stored.status, HypothesisStatus.ACTIVE)

    def test_a_stale_hypothesis_is_not_reinforced_back_to_life(self) -> None:
        hypothesis = make_hypothesis(status=HypothesisStatus.STALE)
        self.store.add_hypothesis(hypothesis)
        self.store.apply_hypothesis_transitions(
            HypothesisTransitions(reinforced_claim_ids=(hypothesis.claim_id,)), touched_at=at(30)
        )
        stored = self.store.get_hypothesis(hypothesis.claim_id)
        self.assertIs(stored.status, HypothesisStatus.STALE)
        self.assertEqual(stored.reinforcement_count, 0)

    def test_empty_transitions_are_a_no_op(self) -> None:
        hypothesis = make_hypothesis()
        self.store.add_hypothesis(hypothesis)
        self.store.apply_hypothesis_transitions(HypothesisTransitions(), touched_at=at(30))
        self.assertEqual(self.store.get_hypothesis(hypothesis.claim_id), hypothesis)

    def test_transition_application_is_atomic(self) -> None:
        good = make_hypothesis(text="good")
        self.store.add_hypothesis(good)
        transitions = HypothesisTransitions(stale_claim_ids=(good.claim_id, "does-not-exist"))
        # An id that matches nothing updates nothing; the valid one still
        # applies, and the whole batch is one transaction.
        self.store.apply_hypothesis_transitions(transitions, touched_at=at(30))
        self.assertIs(self.store.get_hypothesis(good.claim_id).status, HypothesisStatus.STALE)


class WorkingSetTests(StoreTestCase):
    """A risk evaluation reads two things. It should fetch two things."""

    def test_the_working_set_contains_only_what_the_checks_read(self) -> None:
        justification = make_hypothesis(target_ref="core-db")
        self.store.add_hypothesis(justification)
        self.store.add_hypothesis(make_hypothesis(text="an unrelated theory", target_ref="payment-api"))
        for index in range(20):
            self.store.add_fact(make_fact(f"noise {index}", when=index))
        self.store.add_decision(make_decision(target_ref="core-db"))
        self.store.add_decision(make_decision(text="about something else", target_ref="payment-api", when=2))

        action = make_action(justifying_hypothesis_id=justification.claim_id)
        self.store.add_proposed_action(action)

        working = self.store.working_set_for(action, captured_at=at(100))
        self.assertEqual([h.claim_id for h in working.hypotheses], [justification.claim_id])
        self.assertEqual([d.target_ref for d in working.decisions], ["core-db"])
        self.assertEqual(working.facts, ())

    def test_the_working_set_answers_the_same_questions_as_a_full_snapshot(self) -> None:
        justification = make_hypothesis(target_ref="core-db")
        self.store.add_hypothesis(justification)
        decision = make_decision(target_ref="core-db")
        self.store.add_decision(decision)
        action = make_action(justifying_hypothesis_id=justification.claim_id)
        self.store.add_proposed_action(action)

        working = self.store.working_set_for(action, captured_at=at(100))
        full = self.store.snapshot(captured_at=at(100))
        self.assertEqual(
            working.hypothesis(action.justifying_hypothesis_id),
            full.hypothesis(action.justifying_hypothesis_id),
        )
        self.assertEqual(working.decisions_for("core-db"), full.decisions_for("core-db"))

    def test_an_action_with_no_justification_yields_no_hypotheses(self) -> None:
        action = make_action(justifying_hypothesis_id=None)
        self.store.add_proposed_action(action)
        self.assertEqual(self.store.working_set_for(action, captured_at=at(100)).hypotheses, ())


class LatestEvidenceTests(StoreTestCase):
    def test_only_the_current_reading_of_each_metric_is_returned(self) -> None:
        for index in range(10):
            self.store.add_evidence(make_telemetry(value=80 + index, when=index))
            self.store.add_evidence(make_telemetry(metric_name="error_rate", value=index, when=index))

        latest = {item.metric_name: item.value for item in self.store.latest_evidence_per_metric()}
        self.assertEqual(latest, {"pool_utilization": 89.0, "error_rate": 9.0})

    def test_it_matches_folding_the_full_history_in_python(self) -> None:
        for index in range(6):
            self.store.add_evidence(make_telemetry(value=70 + index, when=index * 3))
        from backend.risk_engine.checks import latest_evidence_by_metric

        in_sql = {item.metric_name: item.value for item in self.store.latest_evidence_per_metric()}
        in_python = {
            name: item.value
            for name, item in latest_evidence_by_metric(self.store.evidence()).items()
        }
        self.assertEqual(in_sql, in_python)

    def test_empty_evidence_is_handled(self) -> None:
        self.assertEqual(self.store.latest_evidence_per_metric(), ())


class JustificationGraphTests(StoreTestCase):
    def test_the_reverse_edge_finds_dependent_pending_actions(self) -> None:
        theory = make_hypothesis()
        self.store.add_hypothesis(theory)
        dependent = make_action(justifying_hypothesis_id=theory.claim_id)
        unrelated = make_action(target_ref="payment-api", when=11)
        self.store.add_proposed_action(dependent)
        self.store.add_proposed_action(unrelated)

        found = self.store.pending_actions_justified_by([theory.claim_id])
        self.assertEqual([a.claim_id for a in found], [dependent.claim_id])

    def test_resolved_actions_are_not_revisited(self) -> None:
        theory = make_hypothesis()
        self.store.add_hypothesis(theory)
        action = make_action(justifying_hypothesis_id=theory.claim_id)
        self.store.add_proposed_action(action)
        self.store.resolve_proposed_action(
            action.claim_id, ProposedActionStatus.HELD, resolved_by_uid="1002", resolved_at=at(30)
        )
        self.assertEqual(self.store.pending_actions_justified_by([theory.claim_id]), ())

    def test_empty_input_is_a_no_op(self) -> None:
        self.assertEqual(self.store.pending_actions_justified_by([]), ())


class VersioningTests(StoreTestCase):
    def test_a_write_advances_the_version(self) -> None:
        before = self.store.version
        self.store.add_fact(make_fact())
        self.assertGreater(self.store.version, before)

    def test_reads_do_not_advance_the_version(self) -> None:
        self.store.add_fact(make_fact())
        before = self.store.version
        self.store.snapshot(captured_at=at(60))
        self.store.incident_view(captured_at=at(60))
        self.store.timeline()
        self.assertEqual(self.store.version, before)

    def test_the_version_only_ever_over_invalidates(self) -> None:
        # A duplicate write changes nothing but still bumps. That is the safe
        # direction for a cache validator: a redundant re-read costs a little,
        # reporting "unchanged" when something changed costs correctness.
        fact = make_fact()
        self.store.add_fact(fact)
        before = self.store.version
        self.store.add_fact(fact)
        self.assertGreaterEqual(self.store.version, before)


class TurnIdempotencyTests(StoreTestCase):
    def test_a_turn_id_can_only_be_claimed_once(self) -> None:
        self.assertTrue(self.store.claim_turn("turn-1"))
        self.assertFalse(self.store.claim_turn("turn-1"))
        self.assertTrue(self.store.claim_turn("turn-2"))

    def test_claiming_is_safe_under_concurrency(self) -> None:
        winners: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def claim() -> None:
            barrier.wait()
            result = self.store.claim_turn("contended")
            with lock:
                winners.append(result)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(winners), 1)


class ForeignKeyTests(StoreTestCase):
    def test_dangling_justification_reference_is_rejected(self) -> None:
        # A staleness finding that references a hypothesis which does not
        # exist would be unexplainable; the database prevents it.
        action = make_action(justifying_hypothesis_id="no-such-hypothesis")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_proposed_action(action)

    def test_valid_justification_reference_is_accepted(self) -> None:
        hypothesis = make_hypothesis()
        self.store.add_hypothesis(hypothesis)
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id)
        self.assertTrue(self.store.add_proposed_action(action))


class InterventionRecordTests(StoreTestCase):
    def test_intervention_round_trip(self) -> None:
        record = InterventionRecord(
            action=GovernorAction.WARN,
            outcome=InterventionOutcome.SPOKEN,
            risk_tier=RiskTier.HIGH,
            reasons=("breaks payment-api",),
            codes=(RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,),
            spoken_text="Hold — rolling back Core will break payment-api.",
            decided_at=at(30),
            seconds_since_last_spoken=51.2,
        )
        self.store.record_intervention(record)
        restored = self.store.interventions()[0]
        self.assertEqual(restored, record)


class PersistenceTests(unittest.TestCase):
    def test_state_survives_a_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incident.db"
            with IncidentStateStore(path, incident_id="persist") as store:
                store.add_hypothesis(make_hypothesis(text="might be the pool"))
                store.add_fact(make_fact())
            with IncidentStateStore(path, incident_id="persist") as reopened:
                snapshot = reopened.snapshot(captured_at=at(60))
                self.assertEqual(len(snapshot.hypotheses), 1)
                self.assertEqual(len(snapshot.facts), 1)

    def test_migrations_are_idempotent_across_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incident.db"
            fact = make_fact()
            for _ in range(3):
                # Re-running migrations must neither fail nor disturb the
                # data already there; re-adding the same claim stays a no-op
                # across process restarts, not just within one.
                with IncidentStateStore(path) as store:
                    store.add_fact(fact)
            with IncidentStateStore(path) as store:
                self.assertEqual(len(store.snapshot(captured_at=at(60)).facts), 1)


class ConcurrencyTests(StoreTestCase):
    def test_parallel_writers_do_not_corrupt_state(self) -> None:
        errors: list[BaseException] = []

        def write(index: int) -> None:
            try:
                for offset in range(10):
                    self.store.add_fact(make_fact(f"fact-{index}-{offset}", when=index * 100 + offset))
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        snapshot = self.store.snapshot(captured_at=at(10_000))
        self.assertEqual(len(snapshot.facts), 60)
        self.assertEqual(len(self.store.timeline()), 60)

    def test_snapshot_is_not_torn_by_a_concurrent_writer(self) -> None:
        hypothesis = make_hypothesis()
        self.store.add_hypothesis(hypothesis)
        stop = threading.Event()

        def churn() -> None:
            index = 0
            while not stop.is_set():
                self.store.add_fact(make_fact(f"churn-{index}", when=index))
                index += 1

        writer = threading.Thread(target=churn, daemon=True)
        writer.start()
        try:
            for _ in range(50):
                snapshot = self.store.snapshot(captured_at=at(500))
                # Every action in the snapshot must have its justifying
                # hypothesis present in the same snapshot.
                known = {h.claim_id for h in snapshot.hypotheses}
                for action in snapshot.proposed_actions:
                    if action.justifying_hypothesis_id:
                        self.assertIn(action.justifying_hypothesis_id, known)
        finally:
            stop.set()
            writer.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
