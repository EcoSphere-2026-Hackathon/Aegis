"""
The Intervention Governor.

Deciding *whether to interrupt a live human conversation at all* is the part
of AEGIS that is invisible when it works. The state machine is small; the
constraints around it are what matter.

* **The rate limit is a hard boundary, not a default.** At most one spoken
  intervention per window, with no exception for a second high-risk event
  (SSOT §25 decision #9; Quality Standard §4 red line #6). Bypassing it is a
  hard fail, so the limit is enforced in one place and every path out of
  :meth:`Governor.decide` goes through it.
* **It is measured on a monotonic clock.** Wall-clock time can jump backwards
  on an NTP correction or a suspended laptop, and a limiter built on it can
  be walked straight through its own window.
* **A suppressed verdict is queued, never dropped, and never spoken stale.**
  When the window reopens the queued item is handed back to the caller for
  *re-evaluation against current state* before anything is said -- by then
  the humans may have resolved it themselves, and repeating a warning about
  something already handled is how a system trains people to ignore it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.config import GovernorConfig
from backend.common.enums import (
    GovernorAction,
    InterventionOutcome,
    RiskTier,
)
from backend.common.logging import STAGE_GOVERNOR_DECIDED, get_logger
from backend.common.models import InterventionRecord, RiskVerdict
from backend.governor.speech import build_intervention_text

_log = get_logger("governor")

#: Verdict tier to intervention style.
#:
#: SUGGEST is intentionally unused by the risk tiers: it exists in the state
#: machine for the proactive-nudge feature that the scope lists as
#: if-time-permits, and wiring a risk tier to it now would mean AEGIS
#: volunteering opinions it was not asked for.
_TIER_TO_ACTION: dict[RiskTier, GovernorAction] = {
    RiskTier.LOW: GovernorAction.SILENT,
    RiskTier.MEDIUM: GovernorAction.ASK,
    RiskTier.HIGH: GovernorAction.WARN,
}


@dataclass(frozen=True)
class GovernorDecision:
    """What the Governor decided, and everything needed to explain it."""

    action: GovernorAction
    outcome: InterventionOutcome
    verdict: RiskVerdict
    spoken_text: Optional[str]
    subject_claim_id: Optional[str]
    seconds_since_last_spoken: Optional[float]
    window_open: bool

    @property
    def should_speak(self) -> bool:
        return self.action.is_spoken and self.outcome is InterventionOutcome.SPOKEN

    def to_record(self, *, decided_at) -> InterventionRecord:  # noqa: ANN001 - datetime
        return InterventionRecord(
            action=self.action,
            outcome=self.outcome,
            risk_tier=self.verdict.risk_tier,
            reasons=self.verdict.reasons,
            codes=self.verdict.codes,
            spoken_text=self.spoken_text,
            subject_claim_id=self.subject_claim_id,
            decided_at=decided_at,
            rate_limit_window_open=self.window_open,
            seconds_since_last_spoken=self.seconds_since_last_spoken,
        )


@dataclass
class _QueuedVerdict:
    verdict: RiskVerdict
    subject_claim_id: Optional[str]
    queued_at_monotonic: float


class Governor:
    """Decides whether, and how, to speak."""

    def __init__(
        self,
        config: Optional[GovernorConfig] = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._config = config or GovernorConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self._last_spoken_monotonic: Optional[float] = None
        self._queue: list[_QueuedVerdict] = []
        self._already_voiced: set[tuple[str, ...]] = set()

    # -- state ------------------------------------------------------------

    @property
    def rate_limit_seconds(self) -> float:
        return self._config.rate_limit_seconds

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def seconds_since_last_spoken(self) -> Optional[float]:
        with self._lock:
            if self._last_spoken_monotonic is None:
                return None
            return self._clock.monotonic() - self._last_spoken_monotonic

    def window_is_open(self) -> bool:
        elapsed = self.seconds_since_last_spoken()
        return elapsed is None or elapsed >= self._config.rate_limit_seconds

    def seconds_until_window_opens(self) -> float:
        elapsed = self.seconds_since_last_spoken()
        if elapsed is None:
            return 0.0
        return max(0.0, self._config.rate_limit_seconds - elapsed)

    # -- decisions --------------------------------------------------------

    def decide(
        self, verdict: RiskVerdict, *, subject_claim_id: Optional[str] = None
    ) -> GovernorDecision:
        """Map a verdict to an intervention decision, honouring the limit."""
        action = _TIER_TO_ACTION[verdict.risk_tier]

        with self._lock:
            elapsed = self.seconds_since_last_spoken()
            window_open = self.window_is_open()

            if action is GovernorAction.SILENT:
                decision = GovernorDecision(
                    action=GovernorAction.SILENT,
                    outcome=InterventionOutcome.SUPPRESSED_LOW_RISK,
                    verdict=verdict,
                    spoken_text=None,
                    subject_claim_id=subject_claim_id,
                    seconds_since_last_spoken=elapsed,
                    window_open=window_open,
                )
                self._log_decision(decision)
                return decision

            if not window_open:
                self._enqueue(verdict, subject_claim_id)
                decision = GovernorDecision(
                    action=action,
                    outcome=InterventionOutcome.QUEUED_RATE_LIMITED,
                    verdict=verdict,
                    spoken_text=None,
                    subject_claim_id=subject_claim_id,
                    seconds_since_last_spoken=elapsed,
                    window_open=False,
                )
                self._log_decision(decision)
                return decision

            speakable = self._unvoiced(verdict)
            if speakable is None:
                # Everything in this verdict has already been said aloud.
                # Repeating it spends the one intervention the window allows
                # on information the room already has, and trains people to
                # tune AEGIS out. The verdict is still recorded in full.
                decision = GovernorDecision(
                    action=GovernorAction.SILENT,
                    outcome=InterventionOutcome.SUPPRESSED_ALREADY_SAID,
                    verdict=verdict,
                    spoken_text=None,
                    subject_claim_id=subject_claim_id,
                    seconds_since_last_spoken=elapsed,
                    window_open=True,
                )
                self._log_decision(decision)
                return decision

            # Dropping already-spoken findings can lower the tier of what is
            # left, so the intervention style is recomputed from what will
            # actually be said rather than from the original verdict.
            action = _TIER_TO_ACTION[speakable.risk_tier]
            spoken_text = build_intervention_text(speakable, action)
            self._remember_voiced(speakable)
            self._last_spoken_monotonic = self._clock.monotonic()
            decision = GovernorDecision(
                action=action,
                outcome=InterventionOutcome.SPOKEN,
                verdict=verdict,
                spoken_text=spoken_text,
                subject_claim_id=subject_claim_id,
                seconds_since_last_spoken=elapsed,
                window_open=True,
            )
            self._log_decision(decision)
            return decision

    def _unvoiced(self, verdict: RiskVerdict) -> Optional[RiskVerdict]:
        """The part of this verdict the room has not already heard.

        Returns ``None`` when there is nothing new. Note that dropping
        already-spoken findings can *lower* the tier of what gets said --
        correctly so: if the only remaining news is a MEDIUM concern, a WARN
        would overstate it.
        """
        fresh = [
            finding
            for finding in verdict.findings
            if finding.dedupe_key not in self._already_voiced
        ]
        if not fresh:
            return None
        if len(fresh) == len(verdict.findings):
            return verdict
        return RiskVerdict.from_findings(fresh)

    def _remember_voiced(self, verdict: RiskVerdict) -> None:
        for finding in verdict.findings:
            self._already_voiced.add(finding.dedupe_key)

    def speak_directly(self, text: str) -> bool:
        """Reserve the window for an utterance that is not a risk verdict --
        the on-demand status summary, which a human explicitly asked for.

        Still rate limited. A human asking for status while a warning is
        pending should not be able to consume the window the warning needs.
        """
        with self._lock:
            if not self.window_is_open():
                return False
            self._last_spoken_monotonic = self._clock.monotonic()
            return True

    def release_window(self) -> None:
        """Give the window back after a delivery failure.

        A ``speak`` call that never produced audio must not consume the
        budget -- otherwise one failed HTTP request silences AEGIS for the
        next 45 seconds of a live incident.
        """
        with self._lock:
            self._last_spoken_monotonic = None

    # -- queue ------------------------------------------------------------

    def _enqueue(self, verdict: RiskVerdict, subject_claim_id: Optional[str]) -> None:
        # Deduplicate on subject: a second warning about the same pending
        # action is the same warning, and queueing it twice would spend two
        # windows saying one thing.
        for queued in self._queue:
            if queued.subject_claim_id is not None and queued.subject_claim_id == subject_claim_id:
                queued.verdict = verdict
                queued.queued_at_monotonic = self._clock.monotonic()
                return

        if len(self._queue) >= self._config.max_queue_depth:
            dropped = self._queue.pop(0)
            _log.warning(
                "intervention queue is full; dropping the oldest entry",
                stage=STAGE_GOVERNOR_DECIDED,
                dropped_subject_claim_id=dropped.subject_claim_id,
                max_queue_depth=self._config.max_queue_depth,
            )

        self._queue.append(
            _QueuedVerdict(
                verdict=verdict,
                subject_claim_id=subject_claim_id,
                queued_at_monotonic=self._clock.monotonic(),
            )
        )

    def take_pending(self) -> Optional[tuple[RiskVerdict, Optional[str]]]:
        """Pop the next queued verdict once the window has reopened.

        Returns the verdict *for re-evaluation*, not for speaking. The caller
        must re-run the risk engine against current state before saying
        anything: between queueing and now, the humans may have resolved the
        very thing the warning is about.

        Entries older than the configured maximum age are discarded rather
        than resurfaced -- a warning about a moment that has passed is noise.
        """
        with self._lock:
            if not self.window_is_open():
                return None

            now = self._clock.monotonic()
            while self._queue:
                queued = self._queue.pop(0)
                age = now - queued.queued_at_monotonic
                if age > self._config.queue_max_age_seconds:
                    _log.info(
                        "queued intervention expired before the window reopened",
                        stage=STAGE_GOVERNOR_DECIDED,
                        subject_claim_id=queued.subject_claim_id,
                        age_seconds=round(age, 2),
                        outcome=InterventionOutcome.DROPPED_STALE_ON_REPLAY.value,
                    )
                    continue
                return queued.verdict, queued.subject_claim_id
            return None

    def clear_queue_for(self, subject_claim_id: str) -> int:
        """Drop queued interventions about a subject that has been resolved.

        Called when a human confirms, declines or holds an action: whatever
        AEGIS was waiting to say about it is now moot, and saying it anyway
        would be the system arguing with a decision that has already been
        made.
        """
        with self._lock:
            before = len(self._queue)
            self._queue = [q for q in self._queue if q.subject_claim_id != subject_claim_id]
            removed = before - len(self._queue)
        if removed:
            _log.info(
                "dropped queued interventions for a resolved subject",
                stage=STAGE_GOVERNOR_DECIDED,
                subject_claim_id=subject_claim_id,
                dropped=removed,
            )
        return removed

    # -- logging ----------------------------------------------------------

    def _log_decision(self, decision: GovernorDecision) -> None:
        _log.info(
            "governor decision",
            stage=STAGE_GOVERNOR_DECIDED,
            action=decision.action.value,
            outcome=decision.outcome.value,
            risk_tier=decision.verdict.risk_tier.value,
            codes=[code.value for code in decision.verdict.codes],
            subject_claim_id=decision.subject_claim_id,
            window_open=decision.window_open,
            seconds_since_last_spoken=(
                round(decision.seconds_since_last_spoken, 2)
                if decision.seconds_since_last_spoken is not None
                else None
            ),
            queue_depth=self.queue_depth,
        )
