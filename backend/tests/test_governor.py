"""Governor and speech tests.

The rate limit is a hard red line, so it is tested from several directions:
the ordinary case, the second-high-risk-event case the spec calls out
explicitly, a wall-clock jump, and delivery failure. Speech is tested for the
512-byte budget and for the rule that nothing uncertain may be voiced as
settled.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from backend.common.clock import ManualClock
from backend.common.config import GovernorConfig
from backend.common.enums import (
    GovernorAction,
    InterventionOutcome,
    RiskFindingCode,
    RiskTier,
)
from backend.common.errors import SpeechTooLongError
from backend.common.models import RiskFinding, RiskVerdict
from backend.governor.governor import Governor
from backend.governor.speech import (
    SPEAK_MAX_BYTES,
    build_intervention_text,
    build_status_summary,
)


def finding(tier: RiskTier = RiskTier.HIGH, message: str = "rollback of core-db will break payment-api",
            code: RiskFindingCode = RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK) -> RiskFinding:
    return RiskFinding(code=code, tier=tier, message=message)


def high_verdict(*messages: str) -> RiskVerdict:
    messages = messages or ("rollback of core-db will break payment-api and auth-service",)
    return RiskVerdict.from_findings([finding(RiskTier.HIGH, message) for message in messages])


def medium_verdict(message: str = "the pool root cause still isn't confirmed") -> RiskVerdict:
    return RiskVerdict.from_findings(
        [finding(RiskTier.MEDIUM, message, RiskFindingCode.STALE_JUSTIFICATION)]
    )


class TierMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.governor = Governor(clock=self.clock)

    def test_low_risk_stays_silent(self) -> None:
        decision = self.governor.decide(RiskVerdict.low())
        self.assertIs(decision.action, GovernorAction.SILENT)
        self.assertIs(decision.outcome, InterventionOutcome.SUPPRESSED_LOW_RISK)
        self.assertIsNone(decision.spoken_text)

    def test_medium_risk_asks(self) -> None:
        decision = self.governor.decide(medium_verdict())
        self.assertIs(decision.action, GovernorAction.ASK)
        self.assertTrue(decision.should_speak)

    def test_high_risk_warns(self) -> None:
        decision = self.governor.decide(high_verdict())
        self.assertIs(decision.action, GovernorAction.WARN)
        self.assertTrue(decision.spoken_text.startswith("Hold —"))

    def test_a_silent_decision_does_not_consume_the_window(self) -> None:
        self.governor.decide(RiskVerdict.low())
        self.assertTrue(self.governor.window_is_open())


class RateLimitTests(unittest.TestCase):
    """Quality Standard §4 red line #6."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.governor = Governor(GovernorConfig(rate_limit_seconds=45.0), clock=self.clock)

    def test_second_intervention_inside_the_window_is_queued_not_spoken(self) -> None:
        first = self.governor.decide(high_verdict(), subject_claim_id="a")
        self.assertIs(first.outcome, InterventionOutcome.SPOKEN)

        self.clock.advance(10)
        second = self.governor.decide(high_verdict("something else entirely"), subject_claim_id="b")
        self.assertIs(second.outcome, InterventionOutcome.QUEUED_RATE_LIMITED)
        self.assertIsNone(second.spoken_text)
        self.assertEqual(self.governor.queue_depth, 1)

    def test_a_second_high_risk_event_gets_no_exception(self) -> None:
        # The spec is explicit that even a genuine double-HIGH does not
        # bypass the limit.
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(1)
        for index in range(5):
            decision = self.governor.decide(high_verdict(f"issue {index}"), subject_claim_id=f"s{index}")
            self.assertIs(decision.outcome, InterventionOutcome.QUEUED_RATE_LIMITED)

    def test_window_reopens_exactly_at_the_limit(self) -> None:
        self.governor.decide(high_verdict())
        self.clock.advance(44.9)
        self.assertFalse(self.governor.window_is_open())
        self.clock.advance(0.1)
        self.assertTrue(self.governor.window_is_open())

    def test_a_backwards_wall_clock_jump_cannot_reopen_the_window(self) -> None:
        # A limiter built on wall time could be walked straight through here.
        self.governor.decide(high_verdict())
        self.clock.advance(5)
        self.clock.set_wall_clock(self.clock.now() - timedelta(hours=1))
        self.assertFalse(self.governor.window_is_open())

    def test_seconds_until_window_opens_counts_down(self) -> None:
        self.governor.decide(high_verdict())
        self.clock.advance(20)
        self.assertAlmostEqual(self.governor.seconds_until_window_opens(), 25.0, places=3)

    def test_status_summary_also_respects_the_limit(self) -> None:
        self.governor.decide(high_verdict())
        self.assertFalse(self.governor.speak_directly("status"))
        self.clock.advance(45)
        self.assertTrue(self.governor.speak_directly("status"))

    def test_delivery_failure_returns_the_window(self) -> None:
        self.governor.decide(high_verdict())
        self.assertFalse(self.governor.window_is_open())
        self.governor.release_window()
        self.assertTrue(self.governor.window_is_open())


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.governor = Governor(
            GovernorConfig(rate_limit_seconds=45.0, queue_max_age_seconds=120.0, max_queue_depth=3),
            clock=self.clock,
        )

    def test_queued_verdict_is_returned_for_re_evaluation_when_the_window_opens(self) -> None:
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(5)
        self.governor.decide(high_verdict("second problem"), subject_claim_id="b")

        self.assertIsNone(self.governor.take_pending())  # window still closed
        self.clock.advance(45)
        pending = self.governor.take_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending[1], "b")

    def test_repeated_verdicts_about_one_subject_collapse(self) -> None:
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(1)
        for _ in range(4):
            self.governor.decide(high_verdict("same subject"), subject_claim_id="b")
        self.assertEqual(self.governor.queue_depth, 1)

    def test_expired_entries_are_discarded_rather_than_spoken_late(self) -> None:
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(1)
        self.governor.decide(high_verdict("stale problem"), subject_claim_id="b")
        self.clock.advance(500)
        self.assertIsNone(self.governor.take_pending())
        self.assertEqual(self.governor.queue_depth, 0)

    def test_queue_depth_is_bounded(self) -> None:
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(1)
        for index in range(10):
            self.governor.decide(high_verdict(f"issue {index}"), subject_claim_id=f"s{index}")
        self.assertLessEqual(self.governor.queue_depth, 3)

    def test_resolving_a_subject_clears_its_queued_interventions(self) -> None:
        self.governor.decide(high_verdict(), subject_claim_id="a")
        self.clock.advance(1)
        self.governor.decide(high_verdict("about b"), subject_claim_id="b")
        self.assertEqual(self.governor.clear_queue_for("b"), 1)
        self.assertEqual(self.governor.queue_depth, 0)


class SpeechTests(unittest.TestCase):
    def test_single_finding_reads_as_one_sentence(self) -> None:
        text = build_intervention_text(high_verdict("rolling back core-db will break payment-api"),
                                       GovernorAction.WARN)
        self.assertTrue(text.startswith("Hold —"))
        self.assertIn("payment-api", text)
        self.assertNotIn("two issues", text)

    def test_two_findings_are_announced_and_both_spoken(self) -> None:
        verdict = high_verdict(
            "rolling back core-db will break payment-api and auth-service",
            "the pool root cause still isn't confirmed",
        )
        text = build_intervention_text(verdict, GovernorAction.WARN)
        self.assertIn("two issues", text)
        self.assertIn("payment-api", text)
        self.assertIn("root cause", text)

    def test_output_never_exceeds_the_agora_budget(self) -> None:
        verdict = high_verdict(*[f"finding number {i} with a long explanatory tail" for i in range(12)])
        text = build_intervention_text(verdict, GovernorAction.WARN)
        self.assertLessEqual(len(text.encode("utf-8")), SPEAK_MAX_BYTES)

    def test_reasons_are_never_cut_mid_sentence(self) -> None:
        verdict = high_verdict(*[f"finding {i} " + "x" * 100 for i in range(6)])
        text = build_intervention_text(verdict, GovernorAction.WARN)
        self.assertFalse(text.rstrip().endswith("x."), "a reason was truncated mid-word")
        self.assertLessEqual(len(text.encode("utf-8")), SPEAK_MAX_BYTES)

    def test_the_two_issues_framing_is_dropped_if_only_one_fits(self) -> None:
        long_tail = "y" * 400
        verdict = high_verdict(f"first problem {long_tail}", f"second problem {long_tail}")
        text = build_intervention_text(verdict, GovernorAction.WARN)
        self.assertNotIn("two issues", text)

    def test_an_impossible_single_finding_fails_loudly(self) -> None:
        verdict = high_verdict("z" * 900)
        with self.assertRaises(SpeechTooLongError):
            build_intervention_text(verdict, GovernorAction.WARN)

    def test_ask_tier_uses_a_softer_opener_and_still_hands_back_the_decision(self) -> None:
        text = build_intervention_text(medium_verdict(), GovernorAction.ASK)
        self.assertTrue(text.startswith("Quick check —"))
        self.assertIn("confirm", text.lower())

    def test_warn_always_ends_by_returning_the_decision_to_the_humans(self) -> None:
        text = build_intervention_text(high_verdict(), GovernorAction.WARN)
        self.assertIn("?", text)

    def test_silent_produces_no_speech(self) -> None:
        self.assertEqual(build_intervention_text(RiskVerdict.low(), GovernorAction.SILENT), "")


class StatusSummaryTests(unittest.TestCase):
    def test_open_theories_are_voiced_as_unconfirmed(self) -> None:
        summary = build_status_summary(
            open_hypotheses=["the connection pool is saturated"],
            held_decisions=["hold on the core-db rollback"],
            unresolved_actions=["rollback core-db"],
        )
        self.assertIn("unconfirmed", summary)
        self.assertNotIn("is the cause", summary)

    def test_empty_state_is_described_not_omitted(self) -> None:
        summary = build_status_summary(open_hypotheses=[], held_decisions=[], unresolved_actions=[])
        self.assertIn("No open theories", summary)
        self.assertIn("nothing awaiting a decision", summary)

    def test_summary_respects_the_speak_budget(self) -> None:
        summary = build_status_summary(
            open_hypotheses=[f"theory number {i} about something" for i in range(40)],
            held_decisions=[f"decision {i}" for i in range(40)],
            unresolved_actions=[f"action {i}" for i in range(40)],
        )
        self.assertLessEqual(len(summary.encode("utf-8")), SPEAK_MAX_BYTES)

    def test_singular_and_plural_are_both_grammatical(self) -> None:
        one = build_status_summary(open_hypotheses=["a"], held_decisions=[], unresolved_actions=[])
        two = build_status_summary(open_hypotheses=["a", "b"], held_decisions=[], unresolved_actions=[])
        self.assertIn("1 theory", one)
        self.assertIn("2 theories", two)


if __name__ == "__main__":
    unittest.main()
