"""
Getting a decided intervention to the room.

The orchestrator decides *whether and what* to say. This decides *how that
reaches people and what the record of it looks like* -- the sink call, the
audit row, the console event, and returning the rate-limit window when the
delivery fails. No reasoning happens here and none should: nothing in this
module reads incident state or influences a verdict.

It exists as its own object because of one rule that is easy to state and
easy to violate by accident:

    **Delivery never happens while the state lock is held.**

``sink.speak`` is an HTTP request to Agora with a multi-second timeout, and
it transitions no state -- the governor has already committed the rate-limit
window by the time a decision exists. Held under the lock, one slow response
stalls everything else that touches state: a screenshot submission, a status
request, the next turn. So the orchestrator opens a deferral scope around its
locked section, decisions accumulate in an outbox, and the outbox is flushed
after every lock is released.

The scope is thread-local rather than a parameter threaded through every
private method between the decision and here. That is a deliberate trade: an
``outbox`` argument on six methods would put plumbing in front of the
reasoning a reader came to understand, and the plumbing would still be wrong
if any one of them forgot to pass it on.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Sequence

from backend.common.clock import Clock
from backend.common.errors import InterventionError
from backend.common.logging import STAGE_SPEAK_CALLED, get_logger
from backend.common.metrics import INTERVENTIONS_SPOKEN, STAGE_SPEAK, Metrics
from backend.governor.governor import Governor, GovernorDecision
from backend.pipeline.events import EVENT_INTERVENTION, EventBus
from backend.pipeline.sinks import InterventionSink
from backend.state_store.store import IncidentStateStore

_log = get_logger("delivery")


class InterventionDelivery:
    """Speaks, records and publishes -- outside the caller's state lock."""

    def __init__(
        self,
        *,
        sink: InterventionSink,
        store: IncidentStateStore,
        governor: Governor,
        events: EventBus,
        clock: Clock,
        metrics: Metrics,
    ) -> None:
        self._sink = sink
        self._store = store
        self._governor = governor
        self._events = events
        self._clock = clock
        self._metrics = metrics
        self._scope = threading.local()

    @property
    def sink(self) -> InterventionSink:
        return self._sink

    # -- deferral ---------------------------------------------------------

    @contextmanager
    def deferred(self) -> Iterator[list[Callable[[], None]]]:
        """Collect deliveries instead of performing them.

        Nesting is handled: an inner scope reuses the outer outbox, so
        delivery always happens at the outermost boundary, after every lock
        has been released. Without that, an inner scope would flush while the
        outer one still held the lock -- reintroducing exactly the stall this
        object exists to remove.
        """
        outer = getattr(self._scope, "outbox", None)
        if outer is not None:
            yield outer
            return
        outbox: list[Callable[[], None]] = []
        self._scope.outbox = outbox
        try:
            yield outbox
        finally:
            self._scope.outbox = None

    def flush(self, outbox: Sequence[Callable[[], None]]) -> None:
        """Perform the collected deliveries, in decision order.

        Each is isolated: one sink failure must not abandon the interventions
        decided after it. The handlers below record and log their own
        failures, so nothing is lost by continuing.
        """
        for deliver in outbox:
            try:
                deliver()
            except Exception:  # noqa: BLE001 - one bad delivery is not the turn
                _log.exception("deferred delivery raised", stage=STAGE_SPEAK_CALLED)

    def defer(self, deliver: Callable[[], None]) -> None:
        """Run now, or at the end of the turn when a scope is open."""
        outbox = getattr(self._scope, "outbox", None)
        if outbox is None:
            deliver()
            return
        outbox.append(deliver)

    # -- delivery ---------------------------------------------------------

    def deliver(self, decision: Optional[GovernorDecision]) -> None:
        if decision is None:
            return
        self.defer(lambda: self.deliver_now(decision))

    def speak_plain(self, text: str, *, kind: str) -> None:
        """Deliver an utterance that is not a risk verdict -- the on-demand
        status summary. The governor has already reserved the window."""
        self.defer(lambda: self._speak_plain_now(text, kind=kind))

    def deliver_now(self, decision: GovernorDecision) -> None:
        record = decision.to_record(decided_at=self._clock.now())

        if not decision.should_speak or not decision.spoken_text:
            self._store.record_intervention(record)
            self._publish(decision, spoken=False)
            return

        try:
            with self._metrics.time(STAGE_SPEAK):
                self._sink.speak(decision.spoken_text)
        except InterventionError as exc:
            # A decided-but-undelivered intervention must not also cost the
            # window; releasing it lets the next attempt happen immediately.
            self._governor.release_window()
            self._store.record_intervention(
                record.model_copy(update={"delivery_error": exc.message})
            )
            _log.warning(
                "intervention delivery failed",
                stage=STAGE_SPEAK_CALLED,
                failure_type=type(exc).__name__,
                detail=exc.message,
                subject_claim_id=decision.subject_claim_id,
            )
            self._publish(decision, spoken=False, outcome="delivery_failed", error=exc.message)
            return

        self._store.record_intervention(record)
        self._metrics.increment(INTERVENTIONS_SPOKEN)
        _log.info(
            "intervention spoken",
            stage=STAGE_SPEAK_CALLED,
            action=decision.action.value,
            risk_tier=decision.verdict.risk_tier.value,
            characters=len(decision.spoken_text),
            subject_claim_id=decision.subject_claim_id,
        )
        self._publish(decision, spoken=True, text=decision.spoken_text)

    def _speak_plain_now(self, text: str, *, kind: str) -> None:
        try:
            self._sink.speak(text)
        except InterventionError as exc:
            self._governor.release_window()
            _log.warning(
                "plain utterance could not be delivered",
                stage=STAGE_SPEAK_CALLED,
                intervention_kind=kind,
                failure_type=type(exc).__name__,
                detail=exc.message,
            )
            return
        self._events.publish(
            EVENT_INTERVENTION, intervention_kind=kind, text=text, spoken=True
        )

    def _publish(
        self,
        decision: GovernorDecision,
        *,
        spoken: bool,
        outcome: Optional[str] = None,
        text: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        payload = {
            "action": decision.action.value,
            "outcome": outcome or decision.outcome.value,
            "risk_tier": decision.verdict.risk_tier.value,
            "reasons": list(decision.verdict.reasons),
            "subject_claim_id": decision.subject_claim_id,
            "spoken": spoken,
        }
        if text is not None:
            payload["text"] = text
        if error is not None:
            payload["error"] = error
        self._events.publish(EVENT_INTERVENTION, **payload)
