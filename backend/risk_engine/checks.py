"""
The individual risk checks.

Each is a pure function over typed inputs returning zero or more
:class:`RiskFinding` objects. They are separate functions, not separate
"engines" -- SSOT §25 decision #4 collapses the three originally-pitched
engines into one ``evaluate()``, which composes exactly these.

Two rules hold in every function here:

* **No natural-language interpretation.** Every branch compares typed
  fields. Where meaning has to be read out of English -- is this decision a
  hold or a go-ahead, is this action a rollback -- that reading was done by
  the LLM at extraction time and arrived as an enum. This is the
  ``AI interpretation ≠ deterministic authorization`` boundary, made
  structural rather than aspirational.
* **Missing input is "not evaluated", never "clean".** A check that cannot
  run does not silently return LOW; where the absence itself is risky it
  says so. A rollback whose target version could not be extracted is not a
  safe rollback, it is an unverifiable one.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from backend.common.enums import (
    DecisionStance,
    EvidenceSource,
    ExtractionCertainty,
    HypothesisStatus,
    RiskFindingCode,
    RiskTier,
)
from backend.common.models import (
    Decision,
    Evidence,
    Hypothesis,
    ProposedAction,
    RiskFinding,
    StateSnapshot,
)
from backend.risk_engine.policy import (
    DEFAULT_METRIC_POLICY,
    DEFAULT_STALENESS_POLICY,
    MetricComparisonPolicy,
    StalenessPolicy,
)
from backend.risk_engine.topology import Topology

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def latest_evidence_by_metric(evidence: Iterable[Evidence]) -> Mapping[str, Evidence]:
    """Most recent reading per metric.

    Superseded readings must not contradict a claim: if the pool was at 91%
    two minutes ago and is at 38% now, a claim of "about 40%" is *correct*.
    Comparing against the whole history rather than the latest reading would
    manufacture a contradiction out of stale data -- and then rate-limit away
    a real one.
    """
    newest: dict[str, Evidence] = {}
    for item in evidence:
        current = newest.get(item.metric_name)
        if current is None or item.timestamp > current.timestamp:
            newest[item.metric_name] = item
    return newest


def _format_reading(value: float, unit: Optional[str]) -> str:
    rendered = f"{value:g}"
    if unit is None:
        return rendered
    cleaned = unit.strip()
    if cleaned in {"%", "percent", "percentage", "pct"}:
        return f"{rendered}%"
    return f"{rendered} {cleaned}"


def _humanise_metric(metric_name: str) -> str:
    return metric_name.replace("_", " ")


def _join_names(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"


# ---------------------------------------------------------------------------
# Check 1 -- staleness of the action's justification
# ---------------------------------------------------------------------------


def check_staleness(
    action: ProposedAction,
    state: StateSnapshot,
    evidence: Sequence[Evidence] = (),
    *,
    policy: StalenessPolicy = DEFAULT_STALENESS_POLICY,
) -> list[RiskFinding]:
    """Is this action resting on a theory that has been contradicted, or on
    one nobody ever corroborated?

    Both are the same product failure -- "a hedged guess quietly becomes
    treated as fact" (PRD §2.1) -- and both are reported, at MEDIUM. They
    escalate to HIGH only by compounding with another finding, which is
    exactly the golden demo's beat 6: a stale root cause *and* a blast
    radius, in one intervention.
    """
    hypothesis = state.hypothesis(action.justifying_hypothesis_id)
    if hypothesis is None:
        # Nothing on record justifies this action. That is not the staleness
        # check's business -- an unjustified action is not a stale one.
        return []

    if hypothesis.status is HypothesisStatus.STALE:
        return [
            RiskFinding(
                code=RiskFindingCode.STALE_JUSTIFICATION,
                tier=RiskTier.MEDIUM,
                message=(
                    f"the {_describe_hypothesis(hypothesis)} root cause still isn't confirmed "
                    f"— it was contradicted and never re-established"
                ),
                subject_claim_id=action.claim_id,
                related_ids=(hypothesis.claim_id,),
                detail={
                    "hypothesis_text": hypothesis.text,
                    "hypothesis_status": hypothesis.status.value,
                    "reinforcement_count": hypothesis.reinforcement_count,
                },
            )
        ]

    if policy.require_corroboration and hypothesis.reinforcement_count == 0:
        if not _hypothesis_is_corroborated(hypothesis, evidence, policy.metric):
            return [
                RiskFinding(
                    code=RiskFindingCode.STALE_JUSTIFICATION,
                    tier=RiskTier.MEDIUM,
                    message=(
                        f"the {_describe_hypothesis(hypothesis)} root cause still isn't confirmed "
                        f"— it was stated once and never corroborated"
                    ),
                    subject_claim_id=action.claim_id,
                    related_ids=(hypothesis.claim_id,),
                    detail={
                        "hypothesis_text": hypothesis.text,
                        "hypothesis_status": hypothesis.status.value,
                        "reinforcement_count": 0,
                    },
                )
            ]
    return []


def _describe_hypothesis(hypothesis: Hypothesis) -> str:
    """A short noun phrase for the theory, for use inside a spoken sentence."""
    if hypothesis.metric_ref:
        return _humanise_metric(hypothesis.metric_ref).split()[0]
    if hypothesis.target_ref:
        return hypothesis.target_ref
    words = hypothesis.text.split()
    return " ".join(words[:4]) if words else "stated"


def _hypothesis_is_corroborated(
    hypothesis: Hypothesis,
    evidence: Sequence[Evidence],
    metric_policy: MetricComparisonPolicy,
) -> bool:
    """Does measured reality currently agree with this theory?

    Evidence that agrees with a claim counts as corroboration even when no
    human restated it -- the system should not nag about an unconfirmed
    theory that telemetry is actively confirming.
    """
    if hypothesis.metric_ref is None or hypothesis.claimed_value is None:
        return False
    newest = latest_evidence_by_metric(evidence).get(hypothesis.metric_ref)
    if newest is None:
        return False
    measured = newest.numeric_value
    if measured is None:
        return False
    if not metric_policy.units_comparable(hypothesis.claimed_unit, newest.unit):
        return False
    return metric_policy.values_agree(measured, hypothesis.claimed_value)


# ---------------------------------------------------------------------------
# Check 2 -- decision reversal
# ---------------------------------------------------------------------------


def check_decision_reversal(
    action: ProposedAction,
    state: StateSnapshot,
    evidence: Sequence[Evidence] = (),
) -> list[RiskFinding]:
    """Does this action undo something the team already decided, with no new
    evidence since?

    The decision's polarity is read from ``Decision.stance``, an enum set by
    the extraction service. The previous implementation scanned decision
    prose for words like "don't" and "hold"; that put natural-language
    interpretation inside the deterministic layer and would have missed
    "we're leaving Core alone" entirely.

    Only the *most recent* decision on the target is considered. A team that
    holds, gathers evidence, and then explicitly decides to proceed has not
    reversed anything -- their latest decision stands.
    """
    prior = _latest_decision_before(state.decisions_for(action.target_ref), action)
    if prior is None:
        return []

    if prior.stance is DecisionStance.PROCEED:
        return []  # the action agrees with the standing decision

    if _has_new_evidence_since(prior, action, evidence):
        # New information arrived after the decision, so revisiting it is
        # legitimate incident response, not epistemic drift.
        return []

    if prior.stance is DecisionStance.HOLD:
        return [
            RiskFinding(
                code=RiskFindingCode.DECISION_REVERSAL,
                tier=RiskTier.HIGH,
                message=(
                    f"this reverses the decision to hold on {action.target_ref} "
                    f"(\"{_clip(prior.text)}\") with no new evidence since"
                ),
                subject_claim_id=action.claim_id,
                related_ids=(prior.claim_id,),
                detail={
                    "decision_text": prior.text,
                    "decision_stance": prior.stance.value,
                    "decided_by_uid": prior.speaker_uid,
                    "decided_at": prior.timestamp.isoformat(),
                },
            )
        ]

    # Stance unknown: the extractor logged a decision on this target but
    # could not classify its polarity. Unverifiable, so this asks rather
    # than warns -- the documented preference is to catch too much rather
    # than miss a real reversal (Quality Standard §9).
    return [
        RiskFinding(
            code=RiskFindingCode.DECISION_REVERSAL,
            tier=RiskTier.MEDIUM,
            message=(
                f"there's an earlier decision on {action.target_ref} "
                f"(\"{_clip(prior.text)}\") — worth confirming this doesn't reverse it"
            ),
            subject_claim_id=action.claim_id,
            related_ids=(prior.claim_id,),
            detail={
                "decision_text": prior.text,
                "decision_stance": None,
                "decided_by_uid": prior.speaker_uid,
                "decided_at": prior.timestamp.isoformat(),
            },
        )
    ]


def _latest_decision_before(decisions: Sequence[Decision], action: ProposedAction) -> Optional[Decision]:
    candidates = [d for d in decisions if d.timestamp <= action.timestamp]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.timestamp)


def _has_new_evidence_since(
    decision: Decision,
    action: ProposedAction,
    evidence: Sequence[Evidence],
    policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
) -> bool:
    """Has anything actually changed since the team decided to hold?

    Computed here, deliberately. An earlier version took this as a
    caller-supplied flag, which meant a risk determination was being made
    outside the risk engine.

    The subtlety that matters: AEGIS polls telemetry *as part of evaluating
    this very action*. Treating that poll as "new evidence" would let every
    decision reversal clear itself -- the system would fetch a metric because
    someone proposed something, then cite its own fetch as the new
    information justifying the proposal. Re-reading an unchanged number is
    not new information.

    So a reading only counts when it tells the room something it did not
    already have:

    * a human went and looked (a submitted screenshot), or
    * the metric is one nobody had measured before the decision, or
    * the value has materially moved since the decision.
    """
    # Strictly before the action, not up to and including it. Evidence AEGIS
    # polls while evaluating this action is timestamped at or after the
    # utterance, so a strict bound excludes it by construction rather than by
    # depending on how the clock happens to tick. Anything the room actually
    # saw before speaking lands strictly earlier and still counts.
    relevant = [
        item
        for item in evidence
        if decision.timestamp < item.timestamp < action.timestamp
        and (item.target_ref is None or item.target_ref == action.target_ref)
    ]
    if not relevant:
        return False

    for item in relevant:
        if item.source is EvidenceSource.SCREENSHOT_UPLOAD:
            return True  # somebody actively went and checked

        baseline = _reading_at_or_before(evidence, item.metric_name, decision.timestamp)
        if baseline is None:
            return True  # first measurement of this metric in the incident

        current_value, baseline_value = item.numeric_value, baseline.numeric_value
        if current_value is None or baseline_value is None:
            if str(item.value) != str(baseline.value):
                return True
            continue
        if policy.values_conflict(current_value, baseline_value):
            return True

    return False


def _reading_at_or_before(
    evidence: Sequence[Evidence], metric_name: str, moment
) -> Optional[Evidence]:
    """The most recent reading of a metric as of a point in time."""
    candidates = [
        item for item in evidence if item.metric_name == metric_name and item.timestamp <= moment
    ]
    return max(candidates, key=lambda item: item.timestamp) if candidates else None


def _clip(text: str, limit: int = 60) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Check 3 -- topology blast radius
# ---------------------------------------------------------------------------


def check_blast_radius(action: ProposedAction, topology: Optional[Topology]) -> list[RiskFinding]:
    """What else breaks if this happens?

    Breadth-first traversal of the dependency graph from the action's target,
    then a schema-compatibility test on every dependent the walk reaches.
    Only actions that actually change a schema surface (rollback, migration)
    are evaluated this way: reporting a blast radius for a cache flush would
    burn the one intervention the rate limiter allows every 45 seconds.
    """
    if topology is None or action.target_ref not in topology:
        return []
    if not action.action_kind.changes_schema_surface:
        return []

    paths = topology.blast_radius(action.target_ref)
    schema_readers = [
        path for path in paths if topology.schema_requirement(path.dependent, action.target_ref) is not None
    ]
    if not schema_readers:
        return []

    # People say "roll Core back to the last version", not "to schema v2.3".
    # Where the utterance named a version we use it; otherwise the topology
    # knows what a rollback of this component actually lands on.
    target_version = action.target_schema_version or topology.rollback_target_version(action.target_ref)

    if target_version is None:
        # The action changes a schema surface, dependents read that schema,
        # and we could not determine the target version. Not evaluable --
        # and per the missing-input rule, not therefore safe.
        return [
            RiskFinding(
                code=RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,
                tier=RiskTier.MEDIUM,
                message=(
                    f"I couldn't tell which version {action.target_ref} would move to, and "
                    f"{_join_names([p.dependent for p in schema_readers])} read its schema "
                    f"— compatibility is unverified"
                ),
                subject_claim_id=action.claim_id,
                related_ids=tuple(p.dependent for p in schema_readers),
                detail={
                    "target": action.target_ref,
                    "reason": "target_schema_version_unknown",
                    "schema_readers": [p.dependent for p in schema_readers],
                },
            )
        ]

    broken: list[dict] = []
    for path in schema_readers:
        required = topology.schema_requirement(path.dependent, action.target_ref)
        if required is None:  # filtered above; re-checked rather than asserted
            continue
        if required == target_version:
            continue
        if topology.tolerates(
            path.dependent,
            action.target_ref,
            from_version=required,
            to_version=target_version,
        ):
            continue
        broken.append(
            {
                "node": path.dependent,
                "requires_schema": required,
                "path": path.render(),
                "hops": path.hops,
            }
        )

    if not broken:
        return []

    names = [entry["node"] for entry in broken]
    versions = sorted({str(entry["requires_schema"]) for entry in broken})
    version_phrase = versions[0] if len(versions) == 1 else _join_names(versions)

    # Direct breakage is only the first hop. Anything depending on a broken
    # service is down too, and a failure that reaches an entry point is one a
    # user sees rather than one a graph knows about.
    propagation = topology.propagate_failure(names)

    # The sentence is spoken over people who are already under pressure, and
    # it competes for a 512-byte budget. So the direct breakage is named,
    # and the cascade is *quantified* rather than enumerated: "4 more,
    # including the user-facing api-gateway" lands in a way that reciting six
    # service names does not. The full lists stay in `detail` for the UI and
    # the logs, where there is room to read them.
    message = (
        f"{action.action_kind.value} of {action.target_ref} to {target_version} "
        f"will break {_join_names(names)} — they're on schema {version_phrase}, "
        f"incompatible with {target_version}"
    )
    if propagation.transitive:
        cascade = len(propagation.transitive)
        message += f", cascading to {cascade} more service{'' if cascade == 1 else 's'}"
        if propagation.reaches_users:
            message += f" including user-facing {_join_names(list(propagation.entry_points[:2]))}"
    elif propagation.reaches_users:
        message += f" — {_join_names(list(propagation.entry_points[:2]))} is user-facing"

    return [
        RiskFinding(
            code=RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,
            tier=RiskTier.HIGH,
            message=message,
            subject_claim_id=action.claim_id,
            related_ids=tuple(names),
            detail={
                "target": action.target_ref,
                "target_schema_version": target_version,
                "version_source": "utterance" if action.target_schema_version else "topology",
                "affected": broken,
                "direct_breakage": list(propagation.direct),
                "transitive_breakage": list(propagation.transitive),
                "entry_points_affected": list(propagation.entry_points),
                "total_services_affected": propagation.total,
                "reaches_users": propagation.reaches_users,
            },
        )
    ]


# ---------------------------------------------------------------------------
# Check 4 -- evidence contradiction
# ---------------------------------------------------------------------------


def check_evidence_contradiction(
    *,
    metric_ref: Optional[str],
    claimed_value: Optional[float],
    claimed_unit: Optional[str],
    evidence: Sequence[Evidence],
    subject_claim_id: Optional[str] = None,
    policy: MetricComparisonPolicy = DEFAULT_METRIC_POLICY,
) -> list[RiskFinding]:
    """Does measured reality disagree with what was claimed out loud?

    This is the check that makes AEGIS a grounding system rather than a
    consistency checker: it catches a human being *wrong about the world*,
    not merely inconsistent with themselves (SSOT §5.4).

    Low-certainty visual evidence produces a MEDIUM finding, never HIGH --
    a hard-coded branch on a categorical flag, which is precisely why that
    flag is categorical and not a probability (SSOT §25 decision #14).
    """
    if metric_ref is None or claimed_value is None:
        return []

    newest = latest_evidence_by_metric(evidence).get(metric_ref)
    if newest is None:
        return []

    measured = newest.numeric_value
    if measured is None:
        # A non-numeric reading (e.g. schema_version "v17") cannot be
        # compared against a numeric claim. Not evaluated.
        return []

    if not policy.units_comparable(claimed_unit, newest.unit):
        return []

    if policy.values_agree(measured, claimed_value):
        return []

    is_high_certainty = newest.extraction_certainty is ExtractionCertainty.HIGH
    source_phrase = "telemetry" if newest.source_type.value == "telemetry" else "the screenshot"
    measured_text = _format_reading(measured, newest.unit)
    claimed_text = _format_reading(claimed_value, claimed_unit or newest.unit)

    if is_high_certainty:
        return [
            RiskFinding(
                code=RiskFindingCode.EVIDENCE_CONTRADICTION,
                tier=RiskTier.HIGH,
                message=(
                    f"{source_phrase} shows {_humanise_metric(metric_ref)} at "
                    f"{measured_text}, not {claimed_text}"
                ),
                subject_claim_id=subject_claim_id,
                related_ids=(newest.evidence_id,),
                detail=_contradiction_detail(newest, metric_ref, measured, claimed_value),
            )
        ]

    return [
        RiskFinding(
            code=RiskFindingCode.EVIDENCE_CONTRADICTION_LOW_CERTAINTY,
            tier=RiskTier.MEDIUM,
            message=(
                f"{source_phrase} may show {_humanise_metric(metric_ref)} at "
                f"{measured_text} rather than {claimed_text} — the reading was unclear, "
                f"worth checking directly"
            ),
            subject_claim_id=subject_claim_id,
            related_ids=(newest.evidence_id,),
            detail=_contradiction_detail(newest, metric_ref, measured, claimed_value),
        )
    ]


def _contradiction_detail(
    evidence: Evidence, metric_ref: str, measured: float, claimed: float
) -> dict:
    return {
        "metric": metric_ref,
        "measured_value": measured,
        "claimed_value": claimed,
        "evidence_id": evidence.evidence_id,
        "evidence_source": evidence.source.value,
        "evidence_source_type": evidence.source_type.value,
        "extraction_certainty": evidence.extraction_certainty.value,
        "observed_at": evidence.timestamp.isoformat(),
    }
