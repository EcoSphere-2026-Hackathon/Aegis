"""Hypothesis lifecycle determination tests.

The regression this file exists to lock down: the previous implementation
marked *any* prior hypothesis on the same target stale as soon as a new one
arrived without a numeric value, so a reworded agreement silently killed the
theory it was agreeing with -- and the staleness check then fired on an
action the team had every reason to trust.
"""

from __future__ import annotations

import unittest

from backend.common.enums import HypothesisStatus
from backend.risk_engine.staleness import (
    determine_transitions_from_evidence,
    determine_transitions_from_hypothesis,
)
from backend.tests.support import make_hypothesis, make_telemetry, make_visual_evidence


class TransitionsFromEvidenceTests(unittest.TestCase):
    def test_contradicting_measurement_marks_the_theory_stale(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        transitions = determine_transitions_from_evidence([hypothesis], make_telemetry(value=91))
        self.assertEqual(transitions.stale_claim_ids, (hypothesis.claim_id,))
        self.assertEqual(transitions.reinforced_claim_ids, ())

    def test_agreeing_measurement_reinforces_it(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=90.0, claimed_unit="%"
        )
        transitions = determine_transitions_from_evidence([hypothesis], make_telemetry(value=91))
        self.assertEqual(transitions.reinforced_claim_ids, (hypothesis.claim_id,))
        self.assertEqual(transitions.stale_claim_ids, ())

    def test_evidence_about_another_metric_touches_nothing(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        evidence = make_telemetry(metric_name="error_rate", value=91)
        self.assertTrue(determine_transitions_from_evidence([hypothesis], evidence).is_empty)

    def test_hypothesis_without_a_numeric_claim_is_untouched(self) -> None:
        hypothesis = make_hypothesis(metric_ref="pool_utilization", claimed_value=None)
        transitions = determine_transitions_from_evidence([hypothesis], make_telemetry(value=91))
        self.assertTrue(transitions.is_empty)

    def test_non_numeric_evidence_touches_nothing(self) -> None:
        hypothesis = make_hypothesis(metric_ref="schema_version", claimed_value=17.0)
        evidence = make_telemetry(metric_name="schema_version", value="v17", unit=None)
        self.assertTrue(determine_transitions_from_evidence([hypothesis], evidence).is_empty)

    def test_incomparable_units_touch_nothing(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="p99_latency", claimed_value=2.0, claimed_unit="seconds"
        )
        evidence = make_telemetry(metric_name="p99_latency", value=2000, unit="ms")
        self.assertTrue(determine_transitions_from_evidence([hypothesis], evidence).is_empty)

    def test_already_stale_hypotheses_are_skipped(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization",
            claimed_value=40.0,
            claimed_unit="%",
            status=HypothesisStatus.STALE,
        )
        transitions = determine_transitions_from_evidence([hypothesis], make_telemetry(value=91))
        self.assertTrue(transitions.is_empty)

    def test_low_certainty_visual_evidence_still_determines_transitions(self) -> None:
        # Certainty gates the *intervention* tier, not whether state tracks
        # the observation. Those are separate concerns.
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        transitions = determine_transitions_from_evidence(
            [hypothesis], make_visual_evidence(value=91)
        )
        self.assertEqual(transitions.stale_claim_ids, (hypothesis.claim_id,))


class TransitionsFromHypothesisTests(unittest.TestCase):
    def test_reworded_restatement_does_not_kill_the_original(self) -> None:
        original = make_hypothesis(text="might be the pool", target_ref="core-db")
        restated = make_hypothesis(text="yeah, still think it's the pool", target_ref="core-db", when=30)
        transitions = determine_transitions_from_hypothesis([original], restated)
        self.assertEqual(transitions.stale_claim_ids, ())
        self.assertEqual(transitions.reinforced_claim_ids, (original.claim_id,))

    def test_conflicting_numeric_claim_supersedes(self) -> None:
        original = make_hypothesis(
            target_ref="core-db", metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        revised = make_hypothesis(
            target_ref="core-db", metric_ref="pool_utilization", claimed_value=91.0,
            claimed_unit="%", when=30,
        )
        transitions = determine_transitions_from_hypothesis([original], revised)
        self.assertEqual(transitions.stale_claim_ids, (original.claim_id,))

    def test_agreeing_numeric_claim_reinforces(self) -> None:
        original = make_hypothesis(
            target_ref="core-db", metric_ref="pool_utilization", claimed_value=90.0, claimed_unit="%"
        )
        agreeing = make_hypothesis(
            target_ref="core-db", metric_ref="pool_utilization", claimed_value=91.0,
            claimed_unit="%", when=30,
        )
        transitions = determine_transitions_from_hypothesis([original], agreeing)
        self.assertEqual(transitions.reinforced_claim_ids, (original.claim_id,))

    def test_hypothesis_about_a_different_target_is_untouched(self) -> None:
        original = make_hypothesis(target_ref="payment-api")
        other = make_hypothesis(target_ref="core-db", when=30)
        self.assertTrue(determine_transitions_from_hypothesis([original], other).is_empty)

    def test_new_theory_with_a_figure_does_not_kill_a_figureless_one(self) -> None:
        # Two live theories about one target is a normal incident state; it
        # is not the staleness mechanism's job to prune it.
        original = make_hypothesis(text="could be the pool", target_ref="core-db")
        specific = make_hypothesis(
            text="error rate is about 12%", target_ref="core-db",
            metric_ref="error_rate", claimed_value=12.0, claimed_unit="%", when=30,
        )
        self.assertTrue(determine_transitions_from_hypothesis([original], specific).is_empty)

    def test_a_hypothesis_never_supersedes_itself(self) -> None:
        hypothesis = make_hypothesis(target_ref="core-db")
        self.assertTrue(determine_transitions_from_hypothesis([hypothesis], hypothesis).is_empty)

    def test_matching_by_metric_works_without_a_target(self) -> None:
        original = make_hypothesis(
            target_ref=None, metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        revised = make_hypothesis(
            target_ref=None, metric_ref="pool_utilization", claimed_value=91.0,
            claimed_unit="%", when=30,
        )
        transitions = determine_transitions_from_hypothesis([original], revised)
        self.assertEqual(transitions.stale_claim_ids, (original.claim_id,))


if __name__ == "__main__":
    unittest.main()
