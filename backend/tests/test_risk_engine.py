"""Risk engine tests.

Coverage targets come from Quality Standard §2 (at least one HIGH-triggering
case per check), §7 (edge cases at graph boundaries, held-out topology
scenarios) and §8 (adversarial scenarios). The golden demo's two killer
moments are asserted explicitly, by finding *code* rather than by matching
prose, so rewording an intervention cannot silently break a test that is
supposed to be about behaviour.
"""

from __future__ import annotations

import unittest

from backend.common.enums import (
    ActionKind,
    DecisionStance,
    ExtractionCertainty,
    HypothesisStatus,
    RiskFindingCode,
    RiskTier,
)
from backend.common.errors import ConfigError, VerdictContractError
from backend.common.models import RiskFinding, RiskVerdict
from backend.risk_engine.engine import evaluate, evaluate_claim_grounding
from backend.risk_engine.policy import MetricComparisonPolicy, StalenessPolicy
from backend.risk_engine.topology import (
    GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA,
    Topology,
    build_incident_topology,
)
from backend.tests.support import (
    at,
    make_action,
    make_decision,
    make_hypothesis,
    make_telemetry,
    make_visual_evidence,
    snapshot,
)

import networkx as nx


class TopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = build_incident_topology()

    def test_fixture_is_within_the_specified_size_band(self) -> None:
        self.assertGreaterEqual(self.topology.node_count, 8)
        self.assertLessEqual(self.topology.node_count, 12)

    def test_blast_radius_finds_transitive_dependents_with_paths(self) -> None:
        paths = self.topology.blast_radius("core-db")
        affected = {path.dependent for path in paths}
        # direct
        self.assertIn("payment-api", affected)
        self.assertIn("auth-service", affected)
        # transitive: api-gateway -> payment-api -> core-db
        self.assertIn("api-gateway", affected)
        self.assertIn("billing-service", affected)
        gateway = next(p for p in paths if p.dependent == "api-gateway")
        self.assertEqual(gateway.target, "core-db")
        self.assertGreaterEqual(gateway.hops, 2)
        self.assertTrue(gateway.render().endswith("core-db"))

    def test_blast_radius_is_ordered_nearest_first(self) -> None:
        paths = self.topology.blast_radius("core-db")
        hops = [path.hops for path in paths]
        self.assertEqual(hops, sorted(hops))

    def test_leaf_node_has_no_dependents(self) -> None:
        self.assertEqual(self.topology.blast_radius("search-index"), ())

    def test_unknown_node_returns_empty_rather_than_raising(self) -> None:
        self.assertEqual(self.topology.blast_radius("does-not-exist"), ())

    def test_traversal_terminates_on_a_dependency_cycle(self) -> None:
        graph = nx.MultiDiGraph()
        graph.add_edge("a", "b", key="depends_on", edge_type="depends_on")
        graph.add_edge("b", "c", key="depends_on", edge_type="depends_on")
        graph.add_edge("c", "a", key="depends_on", edge_type="depends_on")
        cyclic = Topology(graph)
        paths = cyclic.blast_radius("a")
        self.assertEqual({p.dependent for p in paths}, {"b", "c"})

    def test_malformed_fixture_is_rejected_at_construction(self) -> None:
        graph = nx.MultiDiGraph()
        graph.add_edge("x", "y", key="depends_on", edge_type="depends_on")
        graph.add_edge("x", "y", key="reads_schema", edge_type="reads_schema")  # no version
        with self.assertRaises(ConfigError):
            Topology(graph)

    def test_schema_reader_without_depends_on_edge_is_rejected(self) -> None:
        # Otherwise the dependent would be invisible to the BFS -- a silent
        # hole in a safety check.
        graph = nx.MultiDiGraph()
        graph.add_edge("x", "y", key="reads_schema", edge_type="reads_schema", schema_version="v1")
        with self.assertRaises(ConfigError):
            Topology(graph)


class StalenessCheckTests(unittest.TestCase):
    def test_action_justified_by_a_stale_hypothesis_is_flagged(self) -> None:
        hypothesis = make_hypothesis(status=HypothesisStatus.STALE, target_ref="core-db")
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id, target_schema_version=None,
                             action_kind=ActionKind.RESTART)
        verdict = evaluate(action, snapshot(hypotheses=[hypothesis]))
        self.assertEqual(verdict.risk_tier, RiskTier.MEDIUM)
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, verdict.codes)

    def test_uncorroborated_hypothesis_is_flagged(self) -> None:
        hypothesis = make_hypothesis(reinforcement_count=0, target_ref="core-db")
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(action, snapshot(hypotheses=[hypothesis]))
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, verdict.codes)

    def test_reinforced_hypothesis_is_not_flagged(self) -> None:
        hypothesis = make_hypothesis(reinforcement_count=2, target_ref="core-db")
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(action, snapshot(hypotheses=[hypothesis]))
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_hypothesis_corroborated_by_agreeing_telemetry_is_not_flagged(self) -> None:
        # Reality confirms the theory; nagging about it being "unconfirmed"
        # would be a false positive that burns the rate-limit window.
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=90.0, claimed_unit="%", target_ref="core-db"
        )
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(
            action, snapshot(hypotheses=[hypothesis]), None, [make_telemetry(value=91)]
        )
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_action_with_no_justification_is_not_a_staleness_finding(self) -> None:
        action = make_action(justifying_hypothesis_id=None, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(action, snapshot())
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_corroboration_requirement_can_be_disabled_by_policy(self) -> None:
        hypothesis = make_hypothesis(reinforcement_count=0, target_ref="core-db")
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(
            action,
            snapshot(hypotheses=[hypothesis]),
            staleness_policy=StalenessPolicy(require_corroboration=False),
        )
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)


class DecisionReversalCheckTests(unittest.TestCase):
    def test_reversing_a_hold_with_no_new_evidence_is_high(self) -> None:
        decision = make_decision(stance=DecisionStance.HOLD, when=5)
        action = make_action(when=10, action_kind=ActionKind.RESTART, target_schema_version=None)
        verdict = evaluate(action, snapshot(decisions=[decision]))
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        self.assertIn(RiskFindingCode.DECISION_REVERSAL, verdict.codes)

    def test_new_evidence_since_the_decision_clears_the_reversal(self) -> None:
        decision = make_decision(stance=DecisionStance.HOLD, when=5)
        action = make_action(when=20, action_kind=ActionKind.RESTART, target_schema_version=None)
        evidence = [make_telemetry(when=10, target_ref="core-db")]
        verdict = evaluate(action, snapshot(decisions=[decision]), None, evidence)
        self.assertNotIn(RiskFindingCode.DECISION_REVERSAL, verdict.codes)

    def test_evidence_predating_the_decision_does_not_clear_it(self) -> None:
        decision = make_decision(stance=DecisionStance.HOLD, when=10)
        action = make_action(when=20, action_kind=ActionKind.RESTART, target_schema_version=None)
        evidence = [make_telemetry(when=5, target_ref="core-db")]
        verdict = evaluate(action, snapshot(decisions=[decision]), None, evidence)
        self.assertIn(RiskFindingCode.DECISION_REVERSAL, verdict.codes)

    def test_a_later_proceed_decision_supersedes_an_earlier_hold(self) -> None:
        hold = make_decision(stance=DecisionStance.HOLD, when=5)
        proceed = make_decision(text="ok, we're doing the rollback", stance=DecisionStance.PROCEED, when=15)
        action = make_action(when=20, action_kind=ActionKind.RESTART, target_schema_version=None)
        verdict = evaluate(action, snapshot(decisions=[hold, proceed]))
        self.assertNotIn(RiskFindingCode.DECISION_REVERSAL, verdict.codes)

    def test_decision_on_a_different_target_is_ignored(self) -> None:
        decision = make_decision(target_ref="payment-api", stance=DecisionStance.HOLD, when=5)
        action = make_action(target_ref="core-db", when=10, action_kind=ActionKind.RESTART,
                             target_schema_version=None)
        verdict = evaluate(action, snapshot(decisions=[decision]))
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_unclassified_decision_stance_asks_rather_than_warns(self) -> None:
        decision = make_decision(stance=None, when=5)
        action = make_action(when=10, action_kind=ActionKind.RESTART, target_schema_version=None)
        verdict = evaluate(action, snapshot(decisions=[decision]))
        self.assertEqual(verdict.risk_tier, RiskTier.MEDIUM)
        self.assertIn(RiskFindingCode.DECISION_REVERSAL, verdict.codes)

    def test_decision_made_after_the_action_is_ignored(self) -> None:
        decision = make_decision(stance=DecisionStance.HOLD, when=30)
        action = make_action(when=10, action_kind=ActionKind.RESTART, target_schema_version=None)
        verdict = evaluate(action, snapshot(decisions=[decision]))
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)


class BlastRadiusCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = build_incident_topology()

    def test_golden_demo_rollback_breaks_exactly_the_two_schema_readers(self) -> None:
        action = make_action(target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA)
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        finding = next(f for f in verdict.findings if f.code is RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK)
        self.assertEqual(set(finding.related_ids), {"payment-api", "auth-service"})
        self.assertIn("payment-api", finding.message)
        self.assertIn("auth-service", finding.message)
        self.assertIn("v17", finding.message)
        self.assertIn("v2.3", finding.message)

    def test_version_tolerant_dependent_is_not_reported(self) -> None:
        action = make_action(target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA)
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertNotIn("cache-layer", " ".join(verdict.reasons))

    def test_schema_agnostic_dependent_is_not_reported(self) -> None:
        action = make_action(target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA)
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertNotIn("analytics-pipeline", " ".join(verdict.reasons))

    def test_rollback_to_the_current_version_breaks_nothing(self) -> None:
        action = make_action(target_schema_version="v17")
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_non_schema_action_does_not_trigger_a_schema_finding(self) -> None:
        action = make_action(action_kind=ActionKind.RESTART, target_schema_version=None)
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_unspoken_rollback_version_is_resolved_from_the_topology(self) -> None:
        # Nobody says "roll back to schema v2.3"; they say "roll back to the
        # last version". The system is expected to know what that means.
        action = make_action(action_kind=ActionKind.ROLLBACK, target_schema_version=None)
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        finding = next(f for f in verdict.findings if f.code is RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK)
        self.assertEqual(finding.detail["target_schema_version"], "v2.3")
        self.assertEqual(finding.detail["version_source"], "topology")

    def test_a_spoken_version_overrides_the_topology_default(self) -> None:
        action = make_action(action_kind=ActionKind.ROLLBACK, target_schema_version="v17")
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_unknown_target_version_is_unverified_not_safe(self) -> None:
        # Neither the utterance nor the topology knows the landing version.
        # Missing input must never be reported as clean.
        graph = nx.MultiDiGraph()
        graph.add_node("db")  # no rollback_schema_version recorded
        graph.add_edge("svc", "db", key="depends_on", edge_type="depends_on")
        graph.add_edge("svc", "db", key="reads_schema", edge_type="reads_schema", schema_version="v9")
        bare = Topology(graph)
        action = make_action(target_ref="db", action_kind=ActionKind.ROLLBACK, target_schema_version=None)
        verdict = evaluate(action, snapshot(), bare)
        self.assertEqual(verdict.risk_tier, RiskTier.MEDIUM)
        self.assertIn(RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK, verdict.codes)
        self.assertEqual(
            next(iter(verdict.findings)).detail["reason"], "target_schema_version_unknown"
        )

    def test_action_on_a_node_outside_the_topology_is_skipped(self) -> None:
        action = make_action(target_ref="unknown-service")
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_leaf_node_rollback_has_no_blast_radius(self) -> None:
        action = make_action(target_ref="search-index", target_schema_version="v9")
        verdict = evaluate(action, snapshot(), self.topology)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_missing_topology_disables_only_that_check(self) -> None:
        action = make_action()
        verdict = evaluate(action, snapshot(), None)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)


class EvidenceContradictionTests(unittest.TestCase):
    def test_golden_demo_beat_three_grounds_a_bare_claim(self) -> None:
        hypothesis = make_hypothesis(
            text="pool utilization looks fine, like 40%",
            metric_ref="pool_utilization",
            claimed_value=40.0,
            claimed_unit="%",
        )
        verdict = evaluate_claim_grounding(hypothesis, [make_telemetry(value=91)])
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        self.assertIn(RiskFindingCode.EVIDENCE_CONTRADICTION, verdict.codes)
        self.assertIn("91", verdict.reasons[0])
        self.assertIn("40", verdict.reasons[0])

    def test_low_certainty_visual_evidence_never_reaches_high(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        verdict = evaluate_claim_grounding(hypothesis, [make_visual_evidence(value=91)])
        self.assertEqual(verdict.risk_tier, RiskTier.MEDIUM)
        self.assertIn(RiskFindingCode.EVIDENCE_CONTRADICTION_LOW_CERTAINTY, verdict.codes)

    def test_high_certainty_visual_evidence_behaves_like_telemetry(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        evidence = make_visual_evidence(value=91, certainty=ExtractionCertainty.HIGH)
        verdict = evaluate_claim_grounding(hypothesis, [evidence])
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)

    def test_agreeing_evidence_produces_no_finding(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=91.0, claimed_unit="%"
        )
        verdict = evaluate_claim_grounding(hypothesis, [make_telemetry(value=91)])
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_value_within_tolerance_is_not_a_contradiction(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=90.0, claimed_unit="%"
        )
        verdict = evaluate_claim_grounding(hypothesis, [make_telemetry(value=91)])
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_only_the_newest_reading_is_compared(self) -> None:
        # Superseded telemetry must not manufacture a contradiction.
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=38.0, claimed_unit="%", when=20
        )
        evidence = [make_telemetry(value=91, when=5), make_telemetry(value=39, when=15)]
        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_mismatched_units_suppress_a_false_contradiction(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="p99_latency", claimed_value=2.0, claimed_unit="seconds"
        )
        evidence = [make_telemetry(metric_name="p99_latency", value=2000, unit="ms")]
        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_equivalent_unit_spellings_still_compare(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="percent"
        )
        verdict = evaluate_claim_grounding(hypothesis, [make_telemetry(value=91, unit="%")])
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)

    def test_non_numeric_evidence_is_not_evaluated(self) -> None:
        hypothesis = make_hypothesis(metric_ref="schema_version", claimed_value=17.0)
        evidence = [make_telemetry(metric_name="schema_version", value="v17", unit=None)]
        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_evidence_about_another_metric_is_ignored(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=40.0, claimed_unit="%"
        )
        evidence = [make_telemetry(metric_name="error_rate", value=91)]
        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self.assertEqual(verdict.risk_tier, RiskTier.LOW)

    def test_custom_tolerance_policy_is_honoured(self) -> None:
        hypothesis = make_hypothesis(
            metric_ref="pool_utilization", claimed_value=85.0, claimed_unit="%"
        )
        strict = MetricComparisonPolicy(relative_tolerance=0.0, absolute_tolerance=0.0)
        verdict = evaluate_claim_grounding(
            hypothesis, [make_telemetry(value=91)], metric_policy=strict
        )
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)


class CompoundCatchTests(unittest.TestCase):
    """SSOT §20 beat 6 -- the second killer moment."""

    def test_stale_root_cause_and_blast_radius_compound_into_one_high_verdict(self) -> None:
        hypothesis = make_hypothesis(
            text="it's the pool",
            status=HypothesisStatus.STALE,
            target_ref="core-db",
            metric_ref="pool_utilization",
            claimed_value=40.0,
            claimed_unit="%",
        )
        action = make_action(
            justifying_hypothesis_id=hypothesis.claim_id,
            target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA,
        )
        verdict = evaluate(
            action,
            snapshot(hypotheses=[hypothesis]),
            build_incident_topology(),
            [make_telemetry(value=91)],
        )
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, verdict.codes)
        self.assertIn(RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK, verdict.codes)
        self.assertIn(RiskFindingCode.EVIDENCE_CONTRADICTION, verdict.codes)
        # highest-tier findings are ordered first, so the spoken line leads
        # with the most severe problem
        self.assertEqual(verdict.findings[0].tier, RiskTier.HIGH)

    def test_two_simultaneous_high_findings_do_not_double_escalate(self) -> None:
        decision = make_decision(stance=DecisionStance.HOLD, when=5)
        action = make_action(when=10, target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA)
        verdict = evaluate(action, snapshot(decisions=[decision]), build_incident_topology())
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        self.assertGreaterEqual(len(verdict.findings), 2)


class VerdictContractTests(unittest.TestCase):
    def test_non_low_verdict_without_findings_is_rejected(self) -> None:
        with self.assertRaises(VerdictContractError):
            RiskVerdict(risk_tier=RiskTier.HIGH, findings=())

    def test_low_verdict_with_findings_is_rejected(self) -> None:
        finding = RiskFinding(
            code=RiskFindingCode.STALE_JUSTIFICATION, tier=RiskTier.MEDIUM, message="x"
        )
        with self.assertRaises(VerdictContractError):
            RiskVerdict(risk_tier=RiskTier.LOW, findings=(finding,))

    def test_declared_tier_must_match_highest_finding(self) -> None:
        finding = RiskFinding(
            code=RiskFindingCode.STALE_JUSTIFICATION, tier=RiskTier.MEDIUM, message="x"
        )
        with self.assertRaises(VerdictContractError):
            RiskVerdict(risk_tier=RiskTier.HIGH, findings=(finding,))

    def test_a_finding_cannot_be_low_tier(self) -> None:
        with self.assertRaises(Exception):
            RiskFinding(code=RiskFindingCode.STALE_JUSTIFICATION, tier=RiskTier.LOW, message="x")

    def test_every_non_low_verdict_explains_itself(self) -> None:
        action = make_action(target_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA)
        verdict = evaluate(action, snapshot(), build_incident_topology())
        self.assertNotEqual(verdict.risk_tier, RiskTier.LOW)
        self.assertTrue(all(reason.strip() for reason in verdict.reasons))


class PurityTests(unittest.TestCase):
    def test_evaluate_does_not_mutate_its_inputs(self) -> None:
        hypothesis = make_hypothesis(status=HypothesisStatus.ACTIVE, target_ref="core-db")
        state = snapshot(hypotheses=[hypothesis])
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id)
        before = state.model_dump_json()
        evaluate(action, state, build_incident_topology(), [make_telemetry()])
        self.assertEqual(state.model_dump_json(), before)
        self.assertIs(state.hypotheses[0].status, HypothesisStatus.ACTIVE)

    def test_evaluate_is_deterministic(self) -> None:
        hypothesis = make_hypothesis(
            status=HypothesisStatus.STALE, target_ref="core-db", metric_ref="pool_utilization",
            claimed_value=40.0, claimed_unit="%",
        )
        action = make_action(justifying_hypothesis_id=hypothesis.claim_id)
        args = (snapshot(hypotheses=[hypothesis]), build_incident_topology(), [make_telemetry()])
        first = evaluate(action, *args)
        second = evaluate(action, *args)
        self.assertEqual(first.model_dump(), second.model_dump())


if __name__ == "__main__":
    unittest.main()
