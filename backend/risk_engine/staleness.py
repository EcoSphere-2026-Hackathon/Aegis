"""
Hypothesis lifecycle determination.

Blueprint §4 c3 is explicit about the division of labour: the risk-evaluation
logic *determines* that a hypothesis has been contradicted or reinforced and
**returns** that determination; the State Store applies it. So this module
computes transitions and writes nothing.

It shares :mod:`backend.risk_engine.policy` with the contradiction check, so
"these two readings disagree" means one thing across the whole system.
"""

from __future__ import annotations

from typing import Sequence

from pydantic import BaseModel, ConfigDict

from backend.common.enums import HypothesisStatus
from backend.common.models import Evidence, Hypothesis
from backend.risk_engine.policy import DEFAULT_METRIC_POLICY, MetricComparisonPolicy


class HypothesisTransitions(BaseModel):
    """What the State Store should apply as a result of one new signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_claim_ids: tuple[str, ...] = ()
    reinforced_claim_ids: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.stale_claim_ids and not self.reinforced_claim_ids


def determine_transitions_from_evidence(
    active_hypotheses: Sequence[Hypothesis],
    new_evidence: Evidence,
    *,
    policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
) -> HypothesisTransitions:
    """A measurement arrived. Which live theories does it kill or confirm?

    Only hypotheses that make a *comparable numeric claim about this metric*
    are touched. Evidence about pool utilisation says nothing about a theory
    concerning DNS, and must not silently invalidate it.
    """
    measured = new_evidence.numeric_value
    if measured is None:
        return HypothesisTransitions()

    stale: list[str] = []
    reinforced: list[str] = []

    for hypothesis in active_hypotheses:
        if hypothesis.status is not HypothesisStatus.ACTIVE:
            continue
        if hypothesis.metric_ref != new_evidence.metric_name:
            continue
        if hypothesis.claimed_value is None:
            continue
        if not policy.units_comparable(hypothesis.claimed_unit, new_evidence.unit):
            continue
        if policy.values_conflict(measured, hypothesis.claimed_value):
            stale.append(hypothesis.claim_id)
        else:
            reinforced.append(hypothesis.claim_id)

    return HypothesisTransitions(
        stale_claim_ids=tuple(stale), reinforced_claim_ids=tuple(reinforced)
    )


def determine_transitions_from_hypothesis(
    active_hypotheses: Sequence[Hypothesis],
    new_hypothesis: Hypothesis,
    *,
    policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
) -> HypothesisTransitions:
    """Someone stated a new theory. Does it reinforce or supersede an old one?

    The rule is deliberately conservative about *superseding*. The previous
    implementation marked any prior hypothesis on the same target stale as
    soon as a new one arrived without a numeric value -- so a reworded
    restatement ("yeah, still think it's the pool") silently killed the
    theory it was agreeing with, and then the staleness check fired on an
    action the team had every reason to trust.

    Now supersession requires *positive evidence of disagreement*: both
    hypotheses make comparable numeric claims about the same metric and
    those claims conflict. Anything weaker is treated as reinforcement if the
    numbers agree, and as an unrelated coexisting theory otherwise. Two live
    theories about one target is a normal state of an incident; it is not the
    staleness mechanism's job to prune it.
    """
    stale: list[str] = []
    reinforced: list[str] = []

    for existing in active_hypotheses:
        if existing.status is not HypothesisStatus.ACTIVE:
            continue
        if existing.claim_id == new_hypothesis.claim_id:
            continue
        if not _same_subject(existing, new_hypothesis):
            continue

        both_numeric = existing.claimed_value is not None and new_hypothesis.claimed_value is not None
        comparable = (
            both_numeric
            and existing.metric_ref is not None
            and existing.metric_ref == new_hypothesis.metric_ref
            and policy.units_comparable(existing.claimed_unit, new_hypothesis.claimed_unit)
        )

        if comparable:
            if policy.values_agree(existing.claimed_value, new_hypothesis.claimed_value):  # type: ignore[arg-type]
                reinforced.append(existing.claim_id)
            else:
                stale.append(existing.claim_id)
            continue

        # Not numerically comparable. Treat a restatement about the same
        # target as reinforcement only when it carries no competing figure;
        # never as supersession.
        if new_hypothesis.claimed_value is None and existing.claimed_value is None:
            reinforced.append(existing.claim_id)

    return HypothesisTransitions(
        stale_claim_ids=tuple(stale), reinforced_claim_ids=tuple(reinforced)
    )


def _same_subject(left: Hypothesis, right: Hypothesis) -> bool:
    """Do two hypotheses talk about the same thing?

    Same named metric, or same target component. Both are structural fields
    set at extraction time -- no text similarity heuristics, which would put
    natural-language judgement back into the deterministic layer.
    """
    if left.metric_ref is not None and left.metric_ref == right.metric_ref:
        return True
    if left.target_ref is not None and left.target_ref == right.target_ref:
        return True
    return False
