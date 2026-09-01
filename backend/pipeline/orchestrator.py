"""
The closed loop.

    utterance -> claims -> state -> evidence -> risk -> governor -> voice
             -> human decision -> state

This module is the only place those components meet, and it owns exactly the
work that belongs between them: routing claims to collections, associating a
proposed action with the theory that justifies it, pulling the evidence a
claim can be grounded against, and applying the determinations the risk
engine returns. It decides no risk itself and speaks no text of its own.

Reliability contract, in one line: **handling an utterance never raises.**
Anything that goes wrong is logged with a failure type and the loop
continues, because a pipeline that dies mid-incident is worse than one that
misses a claim.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.config import PipelineConfig
from backend.common.enums import (
    ClaimType,
    HypothesisStatus,
    ProposedActionStatus,
    RiskTier,
)
from backend.common.errors import (
    AegisError,
    EntityNotFoundError,
    IllegalStateTransitionError,
    InterventionError,
)
from backend.common.logging import (
    STAGE_EVIDENCE_INGESTED,
    STAGE_HUMAN_RESOLUTION,
    STAGE_RISK_EVALUATED,
    STAGE_SPEAK_CALLED,
    STAGE_TRANSCRIPT_RECEIVED,
    correlation_scope,
    get_logger,
)
from backend.common.models import (
    Decision,
    Evidence,
    ExtractedClaim,
    Fact,
    Hypothesis,
    ProposedAction,
    RiskVerdict,
    TranscriptEvent,
)
from backend.extraction.service import ExtractionService
from backend.governor.governor import Governor, GovernorDecision
from backend.governor.speech import build_status_summary
from backend.pipeline.events import (
    EVENT_CLAIM,
    EVENT_EVIDENCE,
    EVENT_INTERVENTION,
    EVENT_RESOLUTION,
    EVENT_RISK_VERDICT,
    EVENT_STATE_CHANGED,
    EVENT_TRANSCRIPT,
    EventBus,
)
from backend.pipeline.sinks import InterventionSink, RecordingSink
from backend.risk_engine.engine import evaluate, evaluate_claim_grounding
from backend.risk_engine.staleness import (
    determine_transitions_from_evidence,
    determine_transitions_from_hypothesis,
)
from backend.risk_engine.topology import Topology
from backend.state_store.store import IncidentStateStore
from backend.telemetry.mock_telemetry import MockTelemetry, TELEMETRY_DEFINITIONS

_log = get_logger("pipeline")

#: Addressing AEGIS directly. Matched on the raw utterance rather than routed
#: through extraction, because "AEGIS, status?" is an interface affordance,
#: not a claim about the incident -- and the extractor would correctly
#: classify it as `none`.
_STATUS_REQUEST = re.compile(
    r"\b(?:aegis|eagis|ages)\b[\s,]*(?:what'?s\s+the\s+)?(?:status|state|summary|update|sitrep)\b",
    re.IGNORECASE,
)

#: How long a telemetry reading may be reused before it is fetched again.
#: Keeps a busy exchange from writing one evidence row per metric per turn
#: while staying far shorter than any interval over which a metric matters.
TELEMETRY_CACHE_SECONDS = 15.0

#: Which component each metric describes, derived from the telemetry
#: catalogue. Used to link a theory about a metric to an action on a
#: component, structurally rather than by guessing from text.
_METRIC_TO_TARGET: dict[str, Optional[str]] = {
    definition.name: definition.target_ref for definition in TELEMETRY_DEFINITIONS
}


@dataclass
class TurnResult:
    """What one utterance produced. Returned for tests, the replay harness
    and the API's synchronous mode; the live path ignores it."""

    event: TranscriptEvent
    claims: tuple[ExtractedClaim, ...] = ()
    verdicts: tuple[RiskVerdict, ...] = ()
    decisions: tuple[GovernorDecision, ...] = ()
    spoken: tuple[str, ...] = ()
    resolved_action_ids: tuple[str, ...] = ()
    degraded: bool = False
    errors: tuple[str, ...] = ()

    @property
    def spoke(self) -> bool:
        return bool(self.spoken)


class IncidentPipeline:
    def __init__(
        self,
        *,
        store: IncidentStateStore,
        extraction: ExtractionService,
        governor: Governor,
        topology: Optional[Topology] = None,
        telemetry: Optional[MockTelemetry] = None,
        sink: Optional[InterventionSink] = None,
        events: Optional[EventBus] = None,
        clock: Clock = SYSTEM_CLOCK,
        config: Optional[PipelineConfig] = None,
    ) -> None:
        self._store = store
        self._extraction = extraction
        self._governor = governor
        self._topology = topology
        self._telemetry = telemetry
        self._sink = sink or RecordingSink(clock=clock)
        self._events = events or EventBus(clock=clock)
        self._clock = clock
        self._config = config or PipelineConfig()
        self._lock = threading.RLock()

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def sink(self) -> InterventionSink:
        return self._sink

    # -- entry point ------------------------------------------------------

    def handle_transcript(self, event: TranscriptEvent) -> TurnResult:
        """Process one transcript event. Never raises."""
        with correlation_scope(event.turn_id):
            if not event.is_actionable:
                _log.debug(
                    "ignoring non-actionable transcript event",
                    stage=STAGE_TRANSCRIPT_RECEIVED,
                    uid=event.uid,
                    final=event.final,
                    role=event.role,
                )
                return TurnResult(event=event)

            _log.info(
                "transcript received",
                stage=STAGE_TRANSCRIPT_RECEIVED,
                uid=event.uid,
                turn_id=event.turn_id,
                characters=len(event.text),
                modality=event.source_modality.value,
            )
            self._events.publish(
                EVENT_TRANSCRIPT,
                uid=event.uid,
                turn_id=event.turn_id,
                text=event.text,
                at=event.timestamp.isoformat(),
                modality=event.source_modality.value,
            )

            try:
                with self._lock:
                    return self._process(event)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                _log.exception("pipeline failed while handling a turn", turn_id=event.turn_id)
                return TurnResult(event=event, errors=(repr(exc),))

    # -- core -------------------------------------------------------------

    def _process(self, event: TranscriptEvent) -> TurnResult:
        if _STATUS_REQUEST.search(event.text):
            return self._handle_status_request(event)

        pending_targets = tuple(action.target_ref for action in self._store.pending_actions())
        outcome = self._extraction.extract(event, pending_action_targets=pending_targets)

        result = TurnResult(event=event, claims=outcome.claims, degraded=outcome.degraded)
        verdicts: list[RiskVerdict] = []
        decisions: list[GovernorDecision] = []
        spoken: list[str] = []
        resolved: list[str] = []

        for claim in outcome.claims:
            if claim.type is ClaimType.NONE:
                continue

            self._events.publish(
                EVENT_CLAIM,
                claim_id=claim.claim_id,
                type=claim.type.value,
                text=claim.text,
                speaker_uid=claim.speaker_uid,
                target_ref=claim.target_ref,
                metric_ref=claim.metric_ref,
            )

            if claim.type is ClaimType.FACT:
                self._store_fact(claim)
            elif claim.type is ClaimType.HYPOTHESIS:
                verdict, decision = self._handle_hypothesis(claim)
                if verdict is not None:
                    verdicts.append(verdict)
                if decision is not None:
                    decisions.append(decision)
                    if decision.spoken_text and decision.should_speak:
                        spoken.append(decision.spoken_text)
            elif claim.type is ClaimType.DECISION:
                self._store_decision(claim)
            elif claim.type is ClaimType.PROPOSED_ACTION:
                verdict, decision = self._handle_proposed_action(claim)
                if verdict is not None:
                    verdicts.append(verdict)
                if decision is not None:
                    decisions.append(decision)
                    if decision.spoken_text and decision.should_speak:
                        spoken.append(decision.spoken_text)
            elif claim.type.is_resolution:
                resolved_id = self._handle_resolution(claim)
                if resolved_id:
                    resolved.append(resolved_id)

        # A window that reopened during this turn may have a queued warning
        # waiting. It is re-evaluated against current state before anything
        # is said -- never replayed verbatim.
        replay = self._replay_queued_intervention()
        if replay is not None:
            decisions.append(replay)
            if replay.spoken_text and replay.should_speak:
                spoken.append(replay.spoken_text)

        self._events.publish(EVENT_STATE_CHANGED, turn_id=event.turn_id)

        return TurnResult(
            event=event,
            claims=outcome.claims,
            verdicts=tuple(verdicts),
            decisions=tuple(decisions),
            spoken=tuple(spoken),
            resolved_action_ids=tuple(resolved),
            degraded=outcome.degraded,
        )

    # -- claim handlers ---------------------------------------------------

    def _store_fact(self, claim: ExtractedClaim) -> None:
        self._store.add_fact(
            Fact(
                claim_id=claim.claim_id,
                text=claim.text,
                speaker_uid=claim.speaker_uid,
                timestamp=claim.timestamp,
                source_turn_id=claim.source_turn_id,
                source_modality=claim.source_modality,
            )
        )

    def _store_decision(self, claim: ExtractedClaim) -> None:
        self._store.add_decision(
            Decision(
                claim_id=claim.claim_id,
                text=claim.text,
                speaker_uid=claim.speaker_uid,
                timestamp=claim.timestamp,
                target_ref=claim.target_ref,
                stance=claim.decision_stance,
                source_turn_id=claim.source_turn_id,
                source_modality=claim.source_modality,
            )
        )

    def _handle_hypothesis(
        self, claim: ExtractedClaim
    ) -> tuple[Optional[RiskVerdict], Optional[GovernorDecision]]:
        """Store a theory, reconcile it with existing ones, and -- if it makes
        a checkable claim about a metric -- ground it against reality.

        This is the path behind the demo's first killer moment: someone
        states a figure from impression, telemetry disagrees, and AEGIS says
        so before anyone acts on it.
        """
        hypothesis = Hypothesis(
            claim_id=claim.claim_id,
            text=claim.text,
            speaker_uid=claim.speaker_uid,
            timestamp=claim.timestamp,
            last_touched_at=claim.timestamp,
            target_ref=claim.target_ref,
            metric_ref=claim.metric_ref,
            claimed_value=claim.claimed_value,
            claimed_unit=claim.claimed_unit,
            source_turn_id=claim.source_turn_id,
            source_modality=claim.source_modality,
        )

        existing_active = self._store.active_hypotheses()
        self._store.add_hypothesis(hypothesis)

        transitions = determine_transitions_from_hypothesis(existing_active, hypothesis)
        if not transitions.is_empty:
            self._store.apply_hypothesis_transitions(transitions, touched_at=claim.timestamp)

        if claim.metric_ref is None or claim.claimed_value is None:
            return None, None

        evidence = self._gather_evidence((claim.metric_ref,))
        if not evidence:
            return None, None

        # Reality may also settle the fate of theories already on record.
        for reading in evidence:
            evidence_transitions = determine_transitions_from_evidence(
                self._store.active_hypotheses(), reading
            )
            if not evidence_transitions.is_empty:
                self._store.apply_hypothesis_transitions(
                    evidence_transitions, touched_at=reading.timestamp
                )

        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self._publish_verdict(verdict, subject_claim_id=hypothesis.claim_id)

        if verdict.risk_tier is RiskTier.LOW:
            return verdict, None

        decision = self._governor.decide(verdict, subject_claim_id=hypothesis.claim_id)
        self._deliver(decision)
        return verdict, decision

    def _handle_proposed_action(
        self, claim: ExtractedClaim
    ) -> tuple[Optional[RiskVerdict], Optional[GovernorDecision]]:
        justification = self._associate_justification(claim)

        action = ProposedAction(
            claim_id=claim.claim_id,
            text=claim.text,
            target_ref=claim.target_ref or "",
            speaker_uid=claim.speaker_uid,
            timestamp=claim.timestamp,
            action_kind=claim.action_kind or _default_action_kind(),
            target_schema_version=claim.target_schema_version,
            justifying_hypothesis_id=justification.claim_id if justification else None,
            source_turn_id=claim.source_turn_id,
            source_modality=claim.source_modality,
        )
        self._store.add_proposed_action(action)

        evidence = self._gather_evidence(self._metrics_relevant_to(action, justification))
        snapshot = self._store.snapshot(captured_at=self._clock.now())
        stored_evidence = self._store.evidence()

        verdict = evaluate(action, snapshot, self._topology, stored_evidence or evidence)
        self._store.attach_risk_verdict(action.claim_id, verdict)
        self._publish_verdict(verdict, subject_claim_id=action.claim_id)

        _log.info(
            "risk evaluated",
            stage=STAGE_RISK_EVALUATED,
            claim_id=action.claim_id,
            target_ref=action.target_ref,
            action_kind=action.action_kind.value,
            risk_tier=verdict.risk_tier.value,
            codes=[code.value for code in verdict.codes],
            justifying_hypothesis_id=action.justifying_hypothesis_id,
        )

        if verdict.risk_tier is RiskTier.LOW:
            return verdict, None

        decision = self._governor.decide(verdict, subject_claim_id=action.claim_id)
        self._deliver(decision)
        return verdict, decision

    def _handle_resolution(self, claim: ExtractedClaim) -> Optional[str]:
        """Apply an explicit human confirm / decline / hold.

        This is the only path by which anything becomes authorised, and it
        requires a classified human utterance. There is deliberately no
        timeout path into this method: an unanswered action stays pending
        indefinitely, which is the correct answer to "nobody replied".
        """
        pending = self._store.pending_actions()
        if not pending:
            _log.info(
                "resolution heard with nothing pending",
                stage=STAGE_HUMAN_RESOLUTION,
                claim_type=claim.type.value,
                speaker_uid=claim.speaker_uid,
            )
            return None

        target = self._select_action_to_resolve(pending, claim)
        if target is None:
            return None

        status = {
            ClaimType.CONFIRMATION: ProposedActionStatus.CONFIRMED,
            ClaimType.OVERRIDE: ProposedActionStatus.DECLINED,
            ClaimType.HOLD: ProposedActionStatus.HELD,
        }[claim.type]

        try:
            resolved = self._store.resolve_proposed_action(
                target.claim_id,
                status,
                resolved_by_uid=claim.speaker_uid,
                resolved_at=claim.timestamp,
            )
        except (EntityNotFoundError, IllegalStateTransitionError) as exc:
            _log.warning(
                "resolution could not be applied",
                stage=STAGE_HUMAN_RESOLUTION,
                claim_id=target.claim_id,
                failure_type=type(exc).__name__,
                detail=exc.message,
            )
            return None

        self._governor.clear_queue_for(target.claim_id)

        _log.info(
            "human resolved a proposed action",
            stage=STAGE_HUMAN_RESOLUTION,
            claim_id=resolved.claim_id,
            status=resolved.status.value,
            resolved_by_uid=resolved.resolved_by_uid,
            target_ref=resolved.target_ref,
        )
        self._events.publish(
            EVENT_RESOLUTION,
            claim_id=resolved.claim_id,
            status=resolved.status.value,
            resolved_by_uid=resolved.resolved_by_uid,
            target_ref=resolved.target_ref,
        )
        return resolved.claim_id

    @staticmethod
    def _select_action_to_resolve(
        pending: Sequence[ProposedAction], claim: ExtractedClaim
    ) -> Optional[ProposedAction]:
        """Which pending action does this reply answer?

        A named target wins; otherwise the most recent pending action, which
        is what "go ahead" means in a conversation. When the reply is
        ambiguous and several are pending, that ambiguity is logged -- it is
        exactly the situation where a wrong guess would authorise the wrong
        thing.
        """
        if claim.target_ref:
            for action in reversed(list(pending)):
                if action.target_ref == claim.target_ref:
                    return action

        if len(pending) > 1:
            _log.warning(
                "ambiguous resolution: several actions are pending",
                stage=STAGE_HUMAN_RESOLUTION,
                pending=[action.claim_id for action in pending],
                chose="most_recent",
                claim_type=claim.type.value,
            )
        return pending[-1]

    # -- supporting concerns ---------------------------------------------

    def _associate_justification(self, claim: ExtractedClaim) -> Optional[Hypothesis]:
        """Which theory is this action resting on?

        Matched structurally, most specific first:

        1. a theory about the same component;
        2. a theory about a metric that describes that component;
        3. failing both, the most recent theory stated inside the association
           window -- which is what "it's the pool, roll Core back" means when
           the two halves arrive in one breath.

        Within equally specific candidates a *stale* theory wins, because an
        action resting on a theory reality already contradicted is precisely
        the failure this product exists to catch.
        """
        target_ref = claim.target_ref
        window_start = claim.timestamp - timedelta(seconds=self._config.stale_after_seconds)

        snapshot = self._store.snapshot(captured_at=claim.timestamp)
        candidates = [
            hypothesis
            for hypothesis in snapshot.hypotheses
            if hypothesis.claim_id != claim.claim_id and hypothesis.timestamp <= claim.timestamp
        ]
        if not candidates:
            return None

        by_target = [h for h in candidates if target_ref and h.target_ref == target_ref]
        by_metric = [
            h
            for h in candidates
            if target_ref and h.metric_ref and _METRIC_TO_TARGET.get(h.metric_ref) == target_ref
        ]
        recent = [h for h in candidates if h.timestamp >= window_start]

        for tier in (by_target, by_metric, recent):
            if tier:
                return max(
                    tier,
                    key=lambda h: (h.status is HypothesisStatus.STALE, h.timestamp),
                )
        return None

    @staticmethod
    def _metrics_relevant_to(
        action: ProposedAction, justification: Optional[Hypothesis]
    ) -> tuple[str, ...]:
        metrics: list[str] = []
        if justification is not None and justification.metric_ref:
            metrics.append(justification.metric_ref)
        for metric, target in _METRIC_TO_TARGET.items():
            if target and target == action.target_ref and metric not in metrics:
                metrics.append(metric)
        return tuple(metrics)

    def _gather_evidence(self, metric_names: Sequence[str]) -> tuple[Evidence, ...]:
        """Fetch and persist readings for the named metrics.

        A reading taken moments ago is reused rather than re-fetched: without
        that, a busy exchange writes one evidence row per metric per turn and
        buries the timeline in duplicates of the same number.
        """
        if self._telemetry is None or not metric_names:
            return ()

        stored = self._store.evidence()
        newest: dict[str, Evidence] = {}
        for item in stored:
            current = newest.get(item.metric_name)
            if current is None or item.timestamp > current.timestamp:
                newest[item.metric_name] = item

        now = self._clock.now()
        gathered: list[Evidence] = []

        for metric_name in metric_names:
            cached = newest.get(metric_name)
            if cached is not None and (now - cached.timestamp).total_seconds() <= TELEMETRY_CACHE_SECONDS:
                gathered.append(cached)
                continue
            try:
                reading = self._telemetry.read(metric_name)
            except AegisError as exc:
                _log.warning(
                    "telemetry read failed; treating the metric as not evaluated",
                    stage=STAGE_EVIDENCE_INGESTED,
                    metric=metric_name,
                    failure_type=type(exc).__name__,
                    detail=exc.message,
                )
                continue
            self._store.add_evidence(reading)
            gathered.append(reading)
            self._events.publish(
                EVENT_EVIDENCE,
                evidence_id=reading.evidence_id,
                metric=reading.metric_name,
                value=reading.value,
                unit=reading.unit,
                source=reading.source.value,
            )
        return tuple(gathered)

    def ingest_evidence(self, evidence: Evidence) -> Optional[GovernorDecision]:
        """Accept externally submitted evidence -- a screenshot reading, or a
        telemetry push -- and reconcile live theories against it.

        Same downstream path as everything else: state, then determination,
        then the one risk engine, then the Governor. Multimodal input never
        gets its own intervention route.
        """
        with self._lock:
            self._store.add_evidence(evidence)
            self._events.publish(
                EVENT_EVIDENCE,
                evidence_id=evidence.evidence_id,
                metric=evidence.metric_name,
                value=evidence.value,
                unit=evidence.unit,
                source=evidence.source.value,
                certainty=evidence.extraction_certainty.value,
            )

            active = self._store.active_hypotheses()
            transitions = determine_transitions_from_evidence(active, evidence)
            if not transitions.is_empty:
                self._store.apply_hypothesis_transitions(transitions, touched_at=evidence.timestamp)

            contradicted = [h for h in active if h.claim_id in transitions.stale_claim_ids]
            if not contradicted:
                return None

            subject = contradicted[-1]
            verdict = evaluate_claim_grounding(subject, self._store.evidence())
            self._publish_verdict(verdict, subject_claim_id=subject.claim_id)
            if verdict.risk_tier is RiskTier.LOW:
                return None

            decision = self._governor.decide(verdict, subject_claim_id=subject.claim_id)
            self._deliver(decision)
            return decision

    def _replay_queued_intervention(self) -> Optional[GovernorDecision]:
        pending = self._governor.take_pending()
        if pending is None:
            return None

        _verdict, subject_claim_id = pending
        if subject_claim_id is None:
            return None

        action = self._store.get_proposed_action(subject_claim_id)
        if action is None:
            return None
        if action.status.is_terminal:
            # The humans dealt with it while AEGIS was rate limited. Saying
            # it now would be arguing with a decision already made.
            _log.info(
                "queued intervention dropped: subject already resolved",
                stage=STAGE_SPEAK_CALLED,
                claim_id=subject_claim_id,
                status=action.status.value,
            )
            return None

        snapshot = self._store.snapshot(captured_at=self._clock.now())
        fresh = evaluate(action, snapshot, self._topology, self._store.evidence())
        if fresh.risk_tier is RiskTier.LOW:
            _log.info(
                "queued intervention dropped: no longer risky on re-evaluation",
                stage=STAGE_SPEAK_CALLED,
                claim_id=subject_claim_id,
            )
            return None

        self._store.attach_risk_verdict(action.claim_id, fresh)
        decision = self._governor.decide(fresh, subject_claim_id=subject_claim_id)
        self._deliver(decision)
        return decision

    def _handle_status_request(self, event: TranscriptEvent) -> TurnResult:
        snapshot = self._store.snapshot(captured_at=self._clock.now())
        summary = build_status_summary(
            open_hypotheses=[_describe_theory(h) for h in snapshot.hypotheses if h.is_active],
            held_decisions=[_describe_decision(d) for d in snapshot.decisions],
            unresolved_actions=[_describe_action(a) for a in snapshot.pending_actions()],
        )

        if not self._governor.speak_directly(summary):
            _log.info(
                "status request deferred by the rate limit",
                stage=STAGE_SPEAK_CALLED,
                seconds_until_open=round(self._governor.seconds_until_window_opens(), 2),
            )
            return TurnResult(event=event)

        try:
            self._sink.speak(summary)
        except InterventionError as exc:
            self._governor.release_window()
            _log.warning(
                "status summary could not be delivered",
                stage=STAGE_SPEAK_CALLED,
                failure_type=type(exc).__name__,
                detail=exc.message,
            )
            return TurnResult(event=event, errors=(exc.message,))

        self._events.publish(EVENT_INTERVENTION, intervention_kind="status", text=summary, spoken=True)
        return TurnResult(event=event, spoken=(summary,))

    # -- delivery ---------------------------------------------------------

    def _deliver(self, decision: GovernorDecision) -> None:
        record = decision.to_record(decided_at=self._clock.now())

        if not decision.should_speak or not decision.spoken_text:
            self._store.record_intervention(record)
            self._events.publish(
                EVENT_INTERVENTION,
                action=decision.action.value,
                outcome=decision.outcome.value,
                risk_tier=decision.verdict.risk_tier.value,
                reasons=list(decision.verdict.reasons),
                subject_claim_id=decision.subject_claim_id,
                spoken=False,
            )
            return

        try:
            self._sink.speak(decision.spoken_text)
        except InterventionError as exc:
            # A decided-but-undelivered intervention must not also cost the
            # window; releasing it lets the next attempt happen immediately.
            self._governor.release_window()
            failed = record.model_copy(
                update={"outcome": record.outcome, "delivery_error": exc.message}
            )
            self._store.record_intervention(failed)
            _log.warning(
                "intervention delivery failed",
                stage=STAGE_SPEAK_CALLED,
                failure_type=type(exc).__name__,
                detail=exc.message,
                subject_claim_id=decision.subject_claim_id,
            )
            self._events.publish(
                EVENT_INTERVENTION,
                action=decision.action.value,
                outcome="delivery_failed",
                risk_tier=decision.verdict.risk_tier.value,
                reasons=list(decision.verdict.reasons),
                subject_claim_id=decision.subject_claim_id,
                spoken=False,
                error=exc.message,
            )
            return

        self._store.record_intervention(record)
        _log.info(
            "intervention spoken",
            stage=STAGE_SPEAK_CALLED,
            action=decision.action.value,
            risk_tier=decision.verdict.risk_tier.value,
            characters=len(decision.spoken_text),
            subject_claim_id=decision.subject_claim_id,
        )
        self._events.publish(
            EVENT_INTERVENTION,
            action=decision.action.value,
            outcome=decision.outcome.value,
            risk_tier=decision.verdict.risk_tier.value,
            reasons=list(decision.verdict.reasons),
            subject_claim_id=decision.subject_claim_id,
            text=decision.spoken_text,
            spoken=True,
        )

    def _publish_verdict(self, verdict: RiskVerdict, *, subject_claim_id: str) -> None:
        self._events.publish(
            EVENT_RISK_VERDICT,
            subject_claim_id=subject_claim_id,
            risk_tier=verdict.risk_tier.value,
            reasons=list(verdict.reasons),
            codes=[code.value for code in verdict.codes],
        )


def _default_action_kind():
    from backend.common.enums import ActionKind

    return ActionKind.OTHER


# ---------------------------------------------------------------------------
# Spoken descriptions
#
# The summary is read aloud, so entries are described by what they are about
# rather than by echoing the sentence someone happened to say. "the pool
# utilization theory" is what a colleague would say; "Okay, fine, it is the
# pool then." is a transcript quote, and reads like one.
# ---------------------------------------------------------------------------


def _describe_theory(hypothesis: Hypothesis) -> str:
    if hypothesis.metric_ref:
        return f"the {hypothesis.metric_ref.replace('_', ' ')} theory"
    if hypothesis.target_ref:
        return f"the {hypothesis.target_ref} theory"
    return _clip_for_speech(hypothesis.text)


def _describe_decision(decision: Decision) -> str:
    if decision.target_ref and decision.stance is not None:
        verb = "hold on" if decision.stance.value == "hold" else "go ahead with"
        return f"{verb} {decision.target_ref}"
    return _clip_for_speech(decision.text)


def _describe_action(action: ProposedAction) -> str:
    return f"{action.action_kind.value.replace('_', ' ')} {action.target_ref}"


def _clip_for_speech(text: str, limit: int = 60) -> str:
    cleaned = " ".join(text.split()).rstrip(" .;,")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
