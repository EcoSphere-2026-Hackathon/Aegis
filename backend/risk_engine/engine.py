"""
``risk_engine.evaluate()`` -- the only function in AEGIS that makes a
risk/safety determination.

Signature is the frozen one (Blueprint "DATA CONTRACTS"):

    evaluate(proposed_action, state, topology, evidence) -> RiskVerdict

``state`` is a single immutable :class:`StateSnapshot` rather than a bag of
loose keyword arguments, so the engine reads one coherent view of the
incident and call sites cannot accidentally pass a half-assembled one.

Properties this module guarantees, and that the tests assert:

* **Pure.** It reads its arguments and returns a verdict. It never writes to
  the State Store -- not even the staleness determination, which is returned
  for the application layer to apply (Blueprint §4 c3).
* **No LLM.** Not directly, not transitively. Every branch is Python
  comparing typed fields.
* **Explained.** A non-LOW verdict always carries at least one finding, and
  every finding names the specific rule, node or reading that produced it.
  The contract is enforced in :class:`RiskVerdict` itself, by raising --
  not by an ``assert``, which ``python -O`` would remove.
* **Fail-toward-catching.** A check whose inputs are missing does not report
  safety. Where the absence is itself risky, it says so at MEDIUM.
"""

from __future__ import annotations

from typing import Optional, Sequence

from backend.common.models import (
    Evidence,
    Hypothesis,
    ProposedAction,
    RiskFinding,
    RiskVerdict,
    StateSnapshot,
)
from backend.risk_engine.checks import (
    check_blast_radius,
    check_decision_reversal,
    check_evidence_contradiction,
    check_staleness,
)
from backend.risk_engine.policy import (
    DEFAULT_METRIC_POLICY,
    DEFAULT_STALENESS_POLICY,
    MetricComparisonPolicy,
    StalenessPolicy,
)
from backend.risk_engine.topology import Topology

__all__ = ["evaluate", "evaluate_claim_grounding"]


def evaluate(
    proposed_action: ProposedAction,
    state: StateSnapshot,
    topology: Optional[Topology] = None,
    evidence: Sequence[Evidence] = (),
    *,
    metric_policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
    staleness_policy: StalenessPolicy = DEFAULT_STALENESS_POLICY,
) -> RiskVerdict:
    """Evaluate one proposed action against incident state, topology and
    evidence.

    The verdict's tier is the highest tier any individual check produced --
    monotonic escalation, so a compound catch (the golden demo's beat 6:
    an unconfirmed root cause *and* a blast radius) reports both reasons at
    the higher tier rather than collapsing to one.
    """
    evidence = tuple(evidence)
    findings: list[RiskFinding] = []

    findings.extend(
        check_staleness(proposed_action, state, evidence, policy=staleness_policy)
    )
    findings.extend(check_decision_reversal(proposed_action, state, evidence))
    findings.extend(check_blast_radius(proposed_action, topology))

    # The action inherits the metric claim of whatever hypothesis justifies
    # it: "it's the pool, roll Core back" is only as sound as the pool claim.
    justification = state.hypothesis(proposed_action.justifying_hypothesis_id)
    if justification is not None:
        findings.extend(
            check_evidence_contradiction(
                metric_ref=justification.metric_ref,
                claimed_value=justification.claimed_value,
                claimed_unit=justification.claimed_unit,
                evidence=evidence,
                subject_claim_id=proposed_action.claim_id,
                policy=metric_policy,
            )
        )

    return RiskVerdict.from_findings(findings)


def evaluate_claim_grounding(
    hypothesis: Hypothesis,
    evidence: Sequence[Evidence] = (),
    *,
    metric_policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
) -> RiskVerdict:
    """Ground a bare spoken claim against measured reality.

    This exists because the golden demo's first killer moment (SSOT §20
    beat 3) fires on a *hypothesis*, before any action has been proposed:
    someone says "pool utilization looks fine, like 40%" and telemetry says
    91%. There is no ``proposed_action`` in play, so ``evaluate()``'s frozen
    signature cannot express it.

    It is not a second engine. It calls the identical
    ``check_evidence_contradiction`` implementation that ``evaluate()``
    calls, returns the identical ``RiskVerdict`` contract, and feeds the
    identical Governor. The only difference is which subject is grounded.
    Checks that are meaningless for a bare claim -- blast radius, decision
    reversal -- are absent because they have nothing to operate on, not
    because they were skipped.
    """
    findings = check_evidence_contradiction(
        metric_ref=hypothesis.metric_ref,
        claimed_value=hypothesis.claimed_value,
        claimed_unit=hypothesis.claimed_unit,
        evidence=tuple(evidence),
        subject_claim_id=hypothesis.claim_id,
        policy=metric_policy,
    )
    return RiskVerdict.from_findings(findings)
