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
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Sequence

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.config import PipelineConfig
from backend.common.enums import (
    ClaimType,
    DecisionStance,
    HypothesisStatus,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
)
from backend.common.errors import (
    AegisError,
    EntityNotFoundError,
    IllegalStateTransitionError,
)
from backend.common.logging import (
    STAGE_EVIDENCE_INGESTED,
    STAGE_HUMAN_RESOLUTION,
    STAGE_RISK_EVALUATED,
    STAGE_SPEAK_CALLED,
    STAGE_STATE_MUTATED,
    STAGE_TRANSCRIPT_RECEIVED,
    correlation_scope,
    get_logger,
)
from backend.common.metrics import (
    EXTRACTION_DEGRADED,
    PROPOSALS_ECHOED,
    REEVALUATIONS_ESCALATED,
    REEVALUATIONS_TRIGGERED,
    RESOLUTIONS_APPLIED,
    RESOLUTIONS_REFUSED,
    STAGE_EXTRACTION,
    STAGE_RISK_EVAL,
    STAGE_TURN_TOTAL,
    STAGE_WORKING_SET,
    TURNS_DEDUPED,
    TURNS_INGESTED,
    Metrics,
)
from backend.common.models import (
    Decision,
    Evidence,
    ExtractedClaim,
    Fact,
    Hypothesis,
    ProposedAction,
    RiskFinding,
    RiskVerdict,
    TranscriptEvent,
)
from backend.extraction.service import ExtractionService
from backend.governor.governor import Governor, GovernorDecision
from backend.governor.speech import build_status_summary
from backend.pipeline.delivery import InterventionDelivery
from backend.pipeline.events import (
    EVENT_CLAIM,
    EVENT_EVIDENCE,
    EVENT_RESOLUTION,
    EVENT_RISK_VERDICT,
    EVENT_STATE_CHANGED,
    EVENT_TRANSCRIPT,
    EventBus,
)
from backend.pipeline.resolution import (
    ResolutionDecision,
    clarification_message,
    select_action_to_resolve,
)
from backend.pipeline.sinks import InterventionSink, RecordingSink
from backend.risk_engine.engine import evaluate, evaluate_claim_grounding
from backend.risk_engine.staleness import (
    determine_transitions_from_evidence,
    determine_transitions_from_hypothesis,
)
from backend.risk_engine.topology import Topology
from backend.state_store.store import IncidentStateStore
from backend.telemetry.mock_telemetry import TELEMETRY_DEFINITIONS, MockTelemetry

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

#: Ceiling on dependent actions re-evaluated when one belief is retracted.
#: A bound rather than a guess: it stops a pathological incident from turning
#: a single invalidation into unbounded work on the ingestion path.
MAX_REEVALUATIONS_PER_TURN = 8

#: Which component each metric describes, derived from the telemetry
#: catalogue. Used to link a theory about a metric to an action on a
#: component, structurally rather than by guessing from text.
_METRIC_TO_TARGET: dict[str, Optional[str]] = {
    definition.name: definition.target_ref for definition in TELEMETRY_DEFINITIONS
}

#: The same relation the other way round, so association can ask the database
#: for "theories about a metric that describes this component" instead of
#: reading every hypothesis and filtering in Python.
_METRICS_DESCRIBING: dict[str, tuple[str, ...]] = {}
for _metric, _target in _METRIC_TO_TARGET.items():
    if _target:
        _METRICS_DESCRIBING[_target] = (*_METRICS_DESCRIBING.get(_target, ()), _metric)


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
    duplicate: bool = False
    """This turn id had already been ingested, so nothing was applied."""

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
        metrics: Optional[Metrics] = None,
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
        self._metrics = metrics or Metrics()
        self._lock = threading.RLock()
        # Built from the resolved dependencies, not the raw arguments: the
        # sink, event bus and metrics registry all have defaults above, and
        # handing the delivery layer the un-defaulted values would give it a
        # different sink from the one this pipeline reports.
        self._delivery = InterventionDelivery(
            sink=self._sink,
            store=self._store,
            governor=self._governor,
            events=self._events,
            clock=self._clock,
            metrics=self._metrics,
        )

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def metrics(self) -> Metrics:
        return self._metrics

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
            # Idempotency belongs *here*, not in one of the callers. The
            # reason it exists is transport redelivery, and the transports
            # differ: HTTP ingest, an RTM relay, the replay harness. A guard
            # that lives in the HTTP handler protects exactly one of them,
            # and the utterance arriving twice through any other path becomes
            # two claims with genuinely different ids that nothing downstream
            # can collapse. One entry point, one claim.
            if not self._store.claim_turn(event.turn_id):
                self._metrics.increment(TURNS_DEDUPED)
                _log.info(
                    "duplicate turn ignored",
                    stage=STAGE_TRANSCRIPT_RECEIVED,
                    turn_id=event.turn_id,
                    uid=event.uid,
                )
                return TurnResult(event=event, duplicate=True)

            self._metrics.increment(TURNS_INGESTED)
            self._events.publish(
                EVENT_TRANSCRIPT,
                uid=event.uid,
                turn_id=event.turn_id,
                text=event.text,
                at=event.timestamp.isoformat(),
                modality=event.source_modality.value,
            )

            try:
                with self._metrics.time(STAGE_TURN_TOTAL):
                    return self._process(event)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                _log.exception("pipeline failed while handling a turn", turn_id=event.turn_id)
                return TurnResult(event=event, errors=(repr(exc),))

    # -- core -------------------------------------------------------------

    def _process(self, event: TranscriptEvent) -> TurnResult:
        if _STATUS_REQUEST.search(event.text):
            with self._delivery.deferred() as outbox, self._lock:
                result = self._handle_status_request(event)
            self._delivery.flush(outbox)
            return result

        # Deduplicated, most-recent first: the model needs to know *which*
        # targets are awaiting a decision, not how many times each was
        # proposed, and a list that grows without bound crowds the prompt.
        pending_targets = tuple(
            dict.fromkeys(
                action.target_ref
                for action in reversed(self._store.pending_actions())
                if action.target_ref
            )
        )

        # Extraction happens *outside* the pipeline lock, deliberately.
        #
        # It is the one genuinely slow step here -- a network call with a
        # multi-second timeout and a retry behind it. Holding the lock across
        # it would mean a stuck provider blocks everything that shares it,
        # including a screenshot submission and an "AEGIS, status?" request,
        # for the full timeout. The lock exists to serialise *state
        # transitions*, and extraction mutates no state; scoping it to the
        # work it actually protects costs nothing and removes a stall that
        # would show up at the worst possible moment.
        with self._metrics.time(STAGE_EXTRACTION):
            outcome = self._extraction.extract(event, pending_action_targets=pending_targets)

        # Speaking is the *other* slow step, and it was still inside the lock:
        # ``sink.speak`` is an HTTP call to Agora with an eight-second
        # timeout. One stalled request would freeze every state transition --
        # screenshot ingestion, status requests, the next turn -- for the
        # whole timeout, in the middle of a live demo. Decisions are made
        # under the lock, where they belong; delivery happens after it.
        with self._delivery.deferred() as outbox, self._lock:
            result = self._apply(event, outcome)
        self._delivery.flush(outbox)
        return result

    def _apply(self, event: TranscriptEvent, outcome) -> TurnResult:  # noqa: ANN001
        if outcome.degraded:
            self._metrics.increment(EXTRACTION_DEGRADED)
        verdicts: list[RiskVerdict] = []
        decisions: list[GovernorDecision] = []
        spoken: list[str] = []
        resolved: list[str] = []

        def _record(decision: Optional[GovernorDecision]) -> None:
            """Every decision is recorded; only a delivered one is 'spoken'.

            The distinction matters: a queued or suppressed decision is still
            part of the turn's audit trail, but reporting it as spoken would
            make the rate limit look breached in every trace and every test.
            """
            if decision is None:
                return
            decisions.append(decision)
            if decision.spoken_text and decision.should_speak:
                spoken.append(decision.spoken_text)

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
                verdict, hypothesis_decisions = self._handle_hypothesis(claim)
                if verdict is not None:
                    verdicts.append(verdict)
                for hypothesis_decision in hypothesis_decisions:
                    _record(hypothesis_decision)
            elif claim.type is ClaimType.DECISION:
                self._store_decision(claim)
            elif claim.type is ClaimType.PROPOSED_ACTION:
                verdict, action_decision = self._handle_proposed_action(claim)
                if verdict is not None:
                    verdicts.append(verdict)
                _record(action_decision)
            elif claim.type.is_resolution:
                resolved_id, clarification = self._handle_resolution(claim)
                if resolved_id:
                    resolved.append(resolved_id)
                _record(clarification)

        # A window that reopened during this turn may have a queued warning
        # waiting. It is re-evaluated against current state before anything
        # is said -- never replayed verbatim.
        _record(self._replay_queued_intervention())

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
    ) -> tuple[Optional[RiskVerdict], list[GovernorDecision]]:
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

        escalations: list[GovernorDecision] = []
        transitions = determine_transitions_from_hypothesis(existing_active, hypothesis)
        if not transitions.is_empty:
            self._store.apply_hypothesis_transitions(transitions, touched_at=claim.timestamp)
            escalations.extend(self._revisit_dependents(transitions.stale_claim_ids))

        if claim.metric_ref is None or claim.claimed_value is None:
            return None, escalations

        evidence = self._gather_evidence((claim.metric_ref,))
        if not evidence:
            return None, escalations

        # Reality may also settle the fate of theories already on record.
        for reading in evidence:
            evidence_transitions = determine_transitions_from_evidence(
                self._store.active_hypotheses(), reading
            )
            if not evidence_transitions.is_empty:
                self._store.apply_hypothesis_transitions(
                    evidence_transitions, touched_at=reading.timestamp
                )
                escalations.extend(self._revisit_dependents(evidence_transitions.stale_claim_ids))

        verdict = evaluate_claim_grounding(hypothesis, evidence)
        self._publish_verdict(verdict, subject_claim_id=hypothesis.claim_id)

        if verdict.risk_tier is RiskTier.LOW:
            return verdict, escalations

        decision = self._governor.decide(verdict, subject_claim_id=hypothesis.claim_id)
        self._delivery.deliver(decision)
        escalations.append(decision)
        return verdict, escalations

    def _handle_proposed_action(
        self, claim: ExtractedClaim
    ) -> tuple[Optional[RiskVerdict], Optional[GovernorDecision]]:
        # A second voice agreeing with a decision already made is an echo, not
        # a new proposal. The words are identical -- "yes, roll back core-db"
        # is both a confirmation and, read alone, a proposal -- so nothing in
        # the utterance separates them; only the state does. Recording the
        # echo would re-open a settled question and show the same rollback as
        # confirmed and pending at the same time.
        kind = claim.action_kind or _default_action_kind()
        echo = self._store.recently_resolved(
            target_ref=claim.target_ref or "",
            action_kind=kind,
            since=claim.timestamp
            - timedelta(seconds=self._config.confirmation_window_seconds),
        )
        if echo is not None:
            _log.info(
                "proposal echoes an action a human already decided",
                stage=STAGE_STATE_MUTATED,
                claim_id=claim.claim_id,
                echoes=echo.claim_id,
                target_ref=echo.target_ref,
                status=echo.status.value,
            )
            self._metrics.increment(PROPOSALS_ECHOED)
            return None, None

        justification = self._associate_justification(claim)

        action = ProposedAction(
            claim_id=claim.claim_id,
            text=claim.text,
            target_ref=claim.target_ref or "",
            speaker_uid=claim.speaker_uid,
            timestamp=claim.timestamp,
            action_kind=kind,
            target_schema_version=claim.target_schema_version,
            justifying_hypothesis_id=justification.claim_id if justification else None,
            source_turn_id=claim.source_turn_id,
            source_modality=claim.source_modality,
        )
        self._store.add_proposed_action(action)

        evidence = self._gather_evidence(self._metrics_relevant_to(action, justification))
        with self._metrics.time(STAGE_WORKING_SET):
            snapshot = self._store.working_set_for(action, captured_at=self._clock.now())
            stored_evidence = self._store.latest_evidence_per_metric()

        with self._metrics.time(STAGE_RISK_EVAL):
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
        self._delivery.deliver(decision)
        return verdict, decision

    def _handle_resolution(
        self, claim: ExtractedClaim
    ) -> tuple[Optional[str], Optional[GovernorDecision]]:
        """Apply an explicit human confirm / decline / hold.

        This is the only path by which anything becomes authorised, and it
        requires a classified human utterance. There is deliberately no
        timeout path into this method: an unanswered action stays pending
        indefinitely, which is the correct answer to "nobody replied".

        Which action a reply answers is decided by :mod:`resolution`, which
        refuses rather than guesses. When it refuses, AEGIS asks -- and the
        action stays pending, because unresolved is the safe resting state.
        """
        pending = self._store.pending_actions()
        decision = select_action_to_resolve(
            pending,
            claim,
            window_seconds=self._config.confirmation_window_seconds,
            last_raised_at=self._store.last_raised_at([a.claim_id for a in pending]),
        )

        if not decision.outcome.is_resolution:
            _log.info(
                "reply did not resolve an action",
                stage=STAGE_HUMAN_RESOLUTION,
                outcome=decision.outcome.value,
                detail=decision.reason,
                claim_type=claim.type.value,
                speaker_uid=claim.speaker_uid,
                candidates=list(decision.candidate_ids),
            )
            self._events.publish(
                EVENT_RESOLUTION,
                claim_id=None,
                status="unresolved",
                outcome=decision.outcome.value,
                reason=decision.reason,
                resolved_by_uid=claim.speaker_uid,
                candidates=list(decision.candidate_ids),
            )
            self._metrics.increment(RESOLUTIONS_REFUSED)
            return None, self._ask_which_action(decision)

        target = decision.action
        assert target is not None  # guaranteed by ResolutionDecision's invariant

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
            return None, None

        self._record_decision_from_resolution(claim, target, status)
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
        self._metrics.increment(RESOLUTIONS_APPLIED)
        return resolved.claim_id, None

    #: A resolution is a human decision, so it belongs in the ledger with the
    #: stance it expresses -- the same place a standalone "we're holding off
    #: on the rollback" lands.
    _RESOLUTION_STANCE = {
        ProposedActionStatus.CONFIRMED: DecisionStance.PROCEED,
        ProposedActionStatus.HELD: DecisionStance.HOLD,
        ProposedActionStatus.DECLINED: DecisionStance.HOLD,
    }

    def _record_decision_from_resolution(
        self,
        claim: ExtractedClaim,
        action: ProposedAction,
        status: ProposedActionStatus,
    ) -> None:
        """Write the decision the reply expressed into the ledger.

        Without this, a hold that arrives as a *reply* to a pending action
        leaves no Decision behind, and the reversal check -- which reads
        decisions, not action statuses -- has nothing to see. The result was
        a silent hole with the exact shape of the spec's second failure
        mode: propose, hear "no, hold off", re-propose thirty seconds later,
        and AEGIS says nothing at all on any target whose blast radius is
        low. Found by tracing a low-risk target through the sequence.

        Recorded under the resolution claim's own id, so the ledger shows who
        said it and when, and the append-only history stays honest about
        which utterance carried the decision.
        """
        stance = self._RESOLUTION_STANCE.get(status)
        if stance is None or not action.target_ref:
            return
        self._store.add_decision(
            Decision(
                claim_id=claim.claim_id,
                text=claim.text,
                speaker_uid=claim.speaker_uid,
                timestamp=claim.timestamp,
                target_ref=action.target_ref,
                stance=stance,
                source_turn_id=claim.source_turn_id,
                source_modality=claim.source_modality,
            )
        )

    def _ask_which_action(
        self, decision: ResolutionDecision
    ) -> Optional[GovernorDecision]:
        """Turn a refused resolution into a question, when there is one to ask.

        Routed through the ordinary governor so it obeys the same rate limit
        as every other spoken intervention and lands in the same audit trail.
        It is explicitly *not* queued: the question only means anything while
        the reply it is about is still the last thing anyone said.
        """
        if not decision.outcome.needs_clarification or not decision.candidates:
            return None

        verdict = RiskVerdict.from_findings(
            [
                RiskFinding(
                    code=RiskFindingCode.AMBIGUOUS_CONFIRMATION,
                    tier=RiskTier.MEDIUM,
                    message=clarification_message(decision),
                    subject_claim_id=decision.candidates[0].claim_id,
                    related_ids=decision.candidate_ids,
                    detail={
                        "outcome": decision.outcome.value,
                        "reason": decision.reason,
                        "candidates": list(decision.candidate_ids),
                    },
                )
            ]
        )
        governor_decision = self._governor.decide(
            verdict,
            subject_claim_id=decision.candidates[0].claim_id,
            queue_if_rate_limited=False,
        )
        self._delivery.deliver(governor_decision)
        return governor_decision

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

        # An indexed union of the three slices below, rather than the whole
        # incident. Reading every hypothesis to pick at most one is the same
        # O(incident) cost the working set exists to remove, and it was still
        # here, on the path taken by every proposed action.
        candidates = [
            hypothesis
            for hypothesis in self._store.hypotheses_for_association(
                target_ref=target_ref,
                metric_refs=_METRICS_DESCRIBING.get(target_ref, ()) if target_ref else (),
                since=window_start,
                until=claim.timestamp,
            )
            if hypothesis.claim_id != claim.claim_id
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
        gets its own intervention route -- including the deferred delivery,
        so a screenshot that triggers an intervention does not hold the state
        lock across the Agora round trip either.
        """
        with self._delivery.deferred() as outbox, self._lock:
            decision = self._ingest_evidence_locked(evidence)
        self._delivery.flush(outbox)
        return decision

    def _ingest_evidence_locked(self, evidence: Evidence) -> Optional[GovernorDecision]:
        """The part that must run under the state lock: everything that
        reads or writes incident state. Delivery is not in here.
        """
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
        verdict = evaluate_claim_grounding(subject, self._store.latest_evidence_per_metric())
        self._publish_verdict(verdict, subject_claim_id=subject.claim_id)
        if verdict.risk_tier is RiskTier.LOW:
            return None

        decision = self._governor.decide(verdict, subject_claim_id=subject.claim_id)
        self._delivery.deliver(decision)
        return decision

    def _revisit_dependents(
        self, invalidated_hypothesis_ids: Sequence[str]
    ) -> list[GovernorDecision]:
        """A belief was retracted. Re-examine what was concluded from it.

        This is the piece that makes AEGIS a reasoning system rather than a
        stream of independent checks. Every proposed action records the
        theory that justifies it, so the store holds a justification graph;
        when reality invalidates a theory, this walks the reverse edge and
        re-evaluates the unresolved actions that were resting on it.

        Without it there is a silent hole with a very bad shape. The team
        proposes a rollback on the strength of a theory. AEGIS evaluates it
        as LOW and stays quiet, correctly. Two turns later telemetry kills
        the theory -- and the rollback is still sitting there, still pending,
        still carrying a verdict computed against a belief nobody holds any
        more. The moment the justification collapses is exactly when somebody
        should say something, and previously nobody did.

        Three properties keep this safe:

        * **It cannot cascade.** Re-evaluating an action produces a verdict;
          it never changes a hypothesis, so this cannot re-trigger itself.
          One level of propagation, always terminating.
        * **It only escalates.** A re-evaluation that comes back the same or
          lower is recorded and stays silent. AEGIS does not get to interrupt
          twice for one problem because a number moved.
        * **It is bounded.** Only unresolved actions, fetched by an indexed
          reverse lookup, capped per turn.
        """
        escalations: list[GovernorDecision] = []
        ids = [claim_id for claim_id in invalidated_hypothesis_ids if claim_id]
        if not ids:
            return escalations

        dependents = self._store.pending_actions_justified_by(ids)
        if not dependents:
            return escalations

        self._metrics.increment(REEVALUATIONS_TRIGGERED, len(dependents))

        for action in dependents[:MAX_REEVALUATIONS_PER_TURN]:
            previous_tier = action.risk_verdict.risk_tier if action.risk_verdict else RiskTier.LOW

            snapshot = self._store.working_set_for(action, captured_at=self._clock.now())
            with self._metrics.time(STAGE_RISK_EVAL):
                fresh = evaluate(
                    action, snapshot, self._topology, self._store.latest_evidence_per_metric()
                )
            self._store.attach_risk_verdict(action.claim_id, fresh)

            if fresh.risk_tier.rank <= previous_tier.rank:
                _log.info(
                    "justification retracted; dependent action re-evaluated, no escalation",
                    stage=STAGE_RISK_EVALUATED,
                    claim_id=action.claim_id,
                    previous_tier=previous_tier.value,
                    risk_tier=fresh.risk_tier.value,
                )
                continue

            self._metrics.increment(REEVALUATIONS_ESCALATED)
            _log.info(
                "justification retracted; dependent action escalated",
                stage=STAGE_RISK_EVALUATED,
                claim_id=action.claim_id,
                previous_tier=previous_tier.value,
                risk_tier=fresh.risk_tier.value,
                codes=[code.value for code in fresh.codes],
            )
            self._publish_verdict(fresh, subject_claim_id=action.claim_id)
            decision = self._governor.decide(fresh, subject_claim_id=action.claim_id)
            self._delivery.deliver(decision)
            escalations.append(decision)

        return escalations

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

        snapshot = self._store.working_set_for(action, captured_at=self._clock.now())
        fresh = evaluate(action, snapshot, self._topology, self._store.latest_evidence_per_metric())
        if fresh.risk_tier is RiskTier.LOW:
            _log.info(
                "queued intervention dropped: no longer risky on re-evaluation",
                stage=STAGE_SPEAK_CALLED,
                claim_id=subject_claim_id,
            )
            return None

        self._store.attach_risk_verdict(action.claim_id, fresh)
        decision = self._governor.decide(fresh, subject_claim_id=subject_claim_id)
        self._delivery.deliver(decision)
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

        self._delivery.speak_plain(summary, kind="status")
        return TurnResult(event=event, spoken=(summary,))

    # -- delivery ---------------------------------------------------------
    #
    # Lives in ``backend.pipeline.delivery``: speaking is an HTTP call with
    # a multi-second timeout and it transitions no state, so it must never
    # happen while the state lock is held. The orchestrator opens a deferral
    # scope around its locked section and flushes after releasing.

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
