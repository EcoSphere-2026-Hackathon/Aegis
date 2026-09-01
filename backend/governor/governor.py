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
from collections import OrderedDict
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


#: What one queued intervention is worth saying, before decay.
#:
#: A HIGH is worth more than two MEDIUMs put together, deliberately: the
#: tiers mean different things, and no quantity of "worth confirming" should
#: outrank one "this breaks two services".
_TIER_UTILITY: dict[RiskTier, float] = {RiskTier.HIGH: 100.0, RiskTier.MEDIUM: 40.0}

#: Each additional independent finding on the same subject adds value. Two
#: separate problems with one action is a stronger reason to spend the
#: channel than one problem is.
_FINDING_BONUS = 8.0


@dataclass
class _QueuedVerdict:
    verdict: RiskVerdict
    subject_claim_id: Optional[str]
    queued_at_monotonic: float

    def base_utility(self) -> float:
        return _TIER_UTILITY.get(self.verdict.risk_tier, 10.0) + _FINDING_BONUS * max(
            0, len(self.verdict.findings) - 1
        )

    def utility(self, now: float, half_life_seconds: float) -> float:
        """Value discounted by how long this has been waiting.

        Relevance decays because an incident moves. A warning about a moment
        that has passed is not merely less useful than a fresh one, it is
        actively bad: it spends the channel telling people about something
        they have moved on from, which is precisely how a system teaches a
        room to tune it out.

        Exponential rather than linear, so nothing ever reaches zero value
        and falls off a cliff at an arbitrary threshold — it just loses to
        anything fresher.
        """
        if half_life_seconds <= 0:
            return self.base_utility()
        age = max(0.0, now - self.queued_at_monotonic)
        return self.base_utility() * (0.5 ** (age / half_life_seconds))


#: How long a spoken finding suppresses the same finding from being said
#: again. Long enough that AEGIS does not repeat itself inside one stretch of
#: conversation; short enough that a concern which resurfaces much later is
#: still allowed to be raised.
VOICED_MEMORY_SECONDS = 900.0

#: Hard ceiling on that memory, so an incident that runs all day cannot grow
#: it without bound.
VOICED_MEMORY_MAX = 512


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
        # Already-voiced findings, oldest first, each with the monotonic
        # moment it was said. A plain set would grow for the length of the
        # incident and, worse, suppress a concern *permanently* -- a
        # contradiction raised in minute three would be unsayable in hour
        # two, when it has become news again.
        self._already_voiced: "OrderedDict[tuple[str, ...], float]" = OrderedDict()
        # Relevance halves over half the retention window, so an entry that
        # has waited most of its life loses to anything recent without ever
        # being discarded on a threshold.
        self._half_life = max(1.0, self._config.queue_max_age_seconds / 2.0)
        self._evicted = 0
        self._preempted = 0
        self._dropped_stale = 0

    # -- state ------------------------------------------------------------

    @property
    def rate_limit_seconds(self) -> float:
        return self._config.rate_limit_seconds

    @property
    def voiced_memory_size(self) -> int:
        """How many findings the governor is currently suppressing as
        already-said. Exposed so the bound can be asserted rather than
        assumed."""
        with self._lock:
            return len(self._already_voiced)

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def scheduling_stats(self) -> dict:
        """What the admission-control policy actually did.

        Exposed because "we prioritise by severity" is a claim, and these
        counters are the evidence for it.
        """
        with self._lock:
            now = self._clock.monotonic()
            return {
                "queue_depth": len(self._queue),
                "evicted_low_value": self._evicted,
                "preempted_by_higher_value": self._preempted,
                "dropped_stale": self._dropped_stale,
                "half_life_seconds": self._half_life,
                "queued": [
                    {
                        "subject_claim_id": item.subject_claim_id,
                        "risk_tier": item.verdict.risk_tier.value,
                        "findings": len(item.verdict.findings),
                        "age_seconds": round(now - item.queued_at_monotonic, 2),
                        "utility": round(item.utility(now, self._half_life), 2),
                    }
                    for item in sorted(
                        self._queue,
                        key=lambda i: i.utility(now, self._half_life),
                        reverse=True,
                    )
                ],
            }

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
        self,
        verdict: RiskVerdict,
        *,
        subject_claim_id: Optional[str] = None,
        queue_if_rate_limited: bool = True,
    ) -> GovernorDecision:
        """Map a verdict to an intervention decision, honouring the limit.

        ``queue_if_rate_limited=False`` is for interventions that are only
        worth saying *now*. Asking "which action did you mean?" forty-five
        seconds after someone said "go ahead" is not a late answer, it is a
        confusing one -- the room has moved on and the question no longer has
        an obvious referent. Such a decision is recorded and dropped rather
        than queued, and nothing is authorised either way, so the safe
        resting state is preserved.
        """
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
                if not queue_if_rate_limited:
                    decision = GovernorDecision(
                        action=GovernorAction.SILENT,
                        outcome=InterventionOutcome.SUPPRESSED_NOT_WORTH_SAYING_LATE,
                        verdict=verdict,
                        spoken_text=None,
                        subject_claim_id=subject_claim_id,
                        seconds_since_last_spoken=elapsed,
                        window_open=False,
                    )
                    self._log_decision(decision)
                    return decision
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
        self._expire_voiced()
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
        now = self._clock.monotonic()
        for finding in verdict.findings:
            self._already_voiced[finding.dedupe_key] = now
            self._already_voiced.move_to_end(finding.dedupe_key)
        self._expire_voiced()

    def _expire_voiced(self) -> None:
        """Forget what was said long enough ago to be worth saying again.

        Bounded twice over: by time, so suppression is a "we just covered
        this" rule rather than a permanent gag, and by count, so a very long
        incident cannot grow this without limit.
        """
        cutoff = self._clock.monotonic() - VOICED_MEMORY_SECONDS
        while self._already_voiced:
            oldest_key = next(iter(self._already_voiced))
            if self._already_voiced[oldest_key] >= cutoff:
                break
            self._already_voiced.pop(oldest_key)
        while len(self._already_voiced) > VOICED_MEMORY_MAX:
            self._already_voiced.popitem(last=False)

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
        now = self._clock.monotonic()

        # Deduplicate on subject: a second warning about the same pending
        # action is the same warning, and queueing it twice would spend two
        # windows saying one thing. The clock is refreshed because the risk
        # was just re-observed, so it is not stale information.
        for queued in self._queue:
            if queued.subject_claim_id is not None and queued.subject_claim_id == subject_claim_id:
                queued.verdict = verdict
                queued.queued_at_monotonic = now
                return

        candidate = _QueuedVerdict(
            verdict=verdict, subject_claim_id=subject_claim_id, queued_at_monotonic=now
        )

        if len(self._queue) >= self._config.max_queue_depth:
            # Evict by *value*, not by arrival order. Dropping the oldest is
            # the obvious policy and it is wrong here: the oldest entry can
            # easily be the most severe one, and a queue that discards
            # "this rollback breaks two services" to make room for two
            # "worth confirming" prompts has inverted the product.
            weakest = min(self._queue, key=lambda item: item.utility(now, self._half_life))
            if weakest.utility(now, self._half_life) >= candidate.utility(now, self._half_life):
                # The newcomer is the least valuable thing in contention, so
                # it is the one that goes.
                self._evicted += 1
                _log.info(
                    "intervention queue full; the new entry was the weakest and was dropped",
                    stage=STAGE_GOVERNOR_DECIDED,
                    subject_claim_id=subject_claim_id,
                    risk_tier=verdict.risk_tier.value,
                    queue_depth=len(self._queue),
                )
                return
            self._queue.remove(weakest)
            self._evicted += 1
            _log.warning(
                "intervention queue full; evicted the lowest-value entry",
                stage=STAGE_GOVERNOR_DECIDED,
                evicted_subject_claim_id=weakest.subject_claim_id,
                evicted_risk_tier=weakest.verdict.risk_tier.value,
                admitted_risk_tier=verdict.risk_tier.value,
                max_queue_depth=self._config.max_queue_depth,
            )

        self._queue.append(candidate)

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

            # Expire first, so nothing stale can win on utility alone.
            fresh: list[_QueuedVerdict] = []
            for queued in self._queue:
                age = now - queued.queued_at_monotonic
                if age > self._config.queue_max_age_seconds:
                    self._dropped_stale += 1
                    _log.info(
                        "queued intervention expired before the window reopened",
                        stage=STAGE_GOVERNOR_DECIDED,
                        subject_claim_id=queued.subject_claim_id,
                        age_seconds=round(age, 2),
                        outcome=InterventionOutcome.DROPPED_STALE_ON_REPLAY.value,
                    )
                    continue
                fresh.append(queued)
            self._queue = fresh

            if not self._queue:
                return None

            # Highest decayed utility wins the channel, not the longest wait.
            #
            # A linear scan rather than a heap, and that is the correct data
            # structure here rather than a lazy one: utility is a function of
            # age, so every key changes on every tick. A binary heap assumes
            # keys are stable while they sit in the structure, so it would
            # silently return the wrong element. At a bounded depth of a
            # couple of dozen entries the scan is free.
            best = max(self._queue, key=lambda item: item.utility(now, self._half_life))
            self._queue.remove(best)

            if self._queue:
                self._preempted += len(self._queue)
                _log.info(
                    "selected the highest-value queued intervention",
                    stage=STAGE_GOVERNOR_DECIDED,
                    chosen_subject_claim_id=best.subject_claim_id,
                    chosen_risk_tier=best.verdict.risk_tier.value,
                    chosen_utility=round(best.utility(now, self._half_life), 2),
                    still_queued=len(self._queue),
                )
            return best.verdict, best.subject_claim_id

    def reset(self) -> None:
        """Forget everything about the incident just finished.

        Not only the queue. A second demo run that inherits a closed
        rate-limit window and a full already-said set produces an AEGIS that
        stays completely silent through the rehearsed script -- the most
        alarming possible demo failure, and one whose cause is invisible from
        the outside.
        """
        with self._lock:
            self._last_spoken_monotonic = None
            self._queue.clear()
            self._already_voiced.clear()
            self._evicted = 0
            self._preempted = 0
            self._dropped_stale = 0

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
