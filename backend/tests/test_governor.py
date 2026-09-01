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
from backend.governor.governor import (
    VOICED_MEMORY_MAX,
    VOICED_MEMORY_SECONDS,
    Governor,
)
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


class SchedulingTests(unittest.TestCase):
    """The channel is scarce: one intervention per window. Which one gets it
    is an admission-control decision, and first-come-first-served is the
    wrong policy for it."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.governor = Governor(
            GovernorConfig(rate_limit_seconds=45.0, queue_max_age_seconds=180.0, max_queue_depth=3),
            clock=self.clock,
        )

    def _occupy_window(self) -> None:
        self.governor.decide(high_verdict("something to occupy the window"), subject_claim_id="occupier")

    def test_severity_wins_the_channel_over_arrival_order(self) -> None:
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(medium_verdict("a minor concern"), subject_claim_id="minor")
        self.clock.advance(1)
        self.governor.decide(high_verdict("a severe blast radius"), subject_claim_id="severe")

        self.clock.advance(45)
        verdict, subject = self.governor.take_pending()
        self.assertEqual(subject, "severe")
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)

    def test_eviction_drops_the_least_valuable_not_the_oldest(self) -> None:
        # The failure this prevents: a severe finding evicted to make room for
        # two "worth confirming" prompts.
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(high_verdict("severe"), subject_claim_id="severe")
        for index in range(4):
            self.governor.decide(medium_verdict(f"minor {index}"), subject_claim_id=f"minor{index}")

        self.clock.advance(45)
        _verdict, subject = self.governor.take_pending()
        self.assertEqual(subject, "severe")

    def test_a_new_low_value_entry_is_refused_rather_than_displacing_a_better_one(self) -> None:
        self._occupy_window()
        self.clock.advance(1)
        for index in range(3):
            self.governor.decide(high_verdict(f"severe {index}"), subject_claim_id=f"severe{index}")
        self.assertEqual(self.governor.queue_depth, 3)

        self.governor.decide(medium_verdict("late minor"), subject_claim_id="late")
        subjects = {entry["subject_claim_id"] for entry in self.governor.scheduling_stats()["queued"]}
        self.assertNotIn("late", subjects)
        self.assertEqual(len(subjects), 3)

    def test_more_independent_findings_outrank_fewer_at_the_same_tier(self) -> None:
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(high_verdict("one problem"), subject_claim_id="single")
        self.governor.decide(
            high_verdict("first problem", "second problem", "third problem"),
            subject_claim_id="compound",
        )
        self.clock.advance(45)
        _verdict, subject = self.governor.take_pending()
        self.assertEqual(subject, "compound")

    def test_relevance_decays_so_a_stale_entry_loses_to_a_fresh_one(self) -> None:
        # A long rate-limit window is the only way to age an entry meaningfully
        # while the channel stays shut -- otherwise the later verdict is simply
        # spoken on arrival and never competes for the queue at all.
        clock = ManualClock()
        governor = Governor(
            GovernorConfig(rate_limit_seconds=120.0, queue_max_age_seconds=120.0),
            clock=clock,
        )
        governor.decide(high_verdict("something to occupy the window"), subject_claim_id="occupier")
        clock.advance(1)
        governor.decide(medium_verdict("aged concern"), subject_claim_id="aged")

        clock.advance(80)  # more than one half-life (60s) of decay
        governor.decide(medium_verdict("fresh concern"), subject_claim_id="fresh")

        clock.advance(40)  # channel reopens; neither entry has expired
        stats = governor.scheduling_stats()
        self.assertEqual(stats["dropped_stale"], 0)
        _verdict, subject = governor.take_pending()
        self.assertEqual(subject, "fresh")

    def test_a_severe_entry_still_beats_a_mild_one_until_it_is_genuinely_old(self) -> None:
        # Decay must not be so aggressive that a HIGH loses to a MEDIUM after
        # a few seconds; severity has to dominate over short horizons.
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(high_verdict("severe"), subject_claim_id="severe")
        self.clock.advance(30)
        self.governor.decide(medium_verdict("fresh minor"), subject_claim_id="minor")

        self.clock.advance(20)
        _verdict, subject = self.governor.take_pending()
        self.assertEqual(subject, "severe")

    def test_scheduling_stats_report_what_the_policy_did(self) -> None:
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(high_verdict("severe"), subject_claim_id="severe")
        self.governor.decide(medium_verdict("minor"), subject_claim_id="minor")

        stats = self.governor.scheduling_stats()
        self.assertEqual(stats["queue_depth"], 2)
        # Reported highest-utility first, so the ordering is inspectable.
        self.assertEqual(stats["queued"][0]["subject_claim_id"], "severe")
        self.assertGreater(stats["queued"][0]["utility"], stats["queued"][1]["utility"])

    def test_expired_entries_never_win_on_utility(self) -> None:
        self._occupy_window()
        self.clock.advance(1)
        self.governor.decide(high_verdict("severe but doomed"), subject_claim_id="doomed")
        self.clock.advance(500)
        self.assertIsNone(self.governor.take_pending())
        self.assertEqual(self.governor.scheduling_stats()["dropped_stale"], 1)


class VoicedMemoryTests(unittest.TestCase):
    """AEGIS must not repeat itself inside one stretch of conversation, and
    must not be permanently gagged on a concern that becomes news again."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.governor = Governor(clock=self.clock)

    def test_the_same_concern_is_not_said_twice_in_a_row(self) -> None:
        self.governor.decide(high_verdict("the pool theory was contradicted"))
        self.clock.advance(60)
        second = self.governor.decide(high_verdict("the pool theory was contradicted"))
        self.assertIsNot(second.action, GovernorAction.WARN)

    def test_a_concern_becomes_sayable_again_much_later(self) -> None:
        # Suppression is "we just covered this", not a permanent gag. A
        # contradiction raised in minute three is news again in hour two.
        self.governor.decide(high_verdict("the pool theory was contradicted"))
        self.clock.advance(VOICED_MEMORY_SECONDS + 60)
        again = self.governor.decide(high_verdict("the pool theory was contradicted"))
        self.assertIs(again.action, GovernorAction.WARN)

    def test_the_memory_is_bounded_over_a_long_incident(self) -> None:
        for index in range(VOICED_MEMORY_MAX * 2):
            self.clock.advance(46)
            self.governor.decide(
                high_verdict(f"distinct concern {index}"), subject_claim_id=f"c{index}"
            )
        self.assertLessEqual(self.governor.voiced_memory_size, VOICED_MEMORY_MAX)


class SpeechBudgetTests(unittest.TestCase):
    """The 512-byte limit makes composing an intervention a packing problem,
    not a truncation problem."""

    def _verdict(self, *specs) -> RiskVerdict:
        codes = [
            RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK,
            RiskFindingCode.DECISION_REVERSAL,
            RiskFindingCode.STALE_JUSTIFICATION,
            RiskFindingCode.EVIDENCE_CONTRADICTION_LOW_CERTAINTY,
        ]
        return RiskVerdict.from_findings(
            [
                RiskFinding(code=codes[index % len(codes)], tier=tier, message=message)
                for index, (tier, message) in enumerate(specs)
            ]
        )

    def test_two_short_findings_are_preferred_over_one_long_one_of_equal_tier(self) -> None:
        # Greedy-by-severity takes whichever equal-tier finding comes first
        # and can then afford nothing else. The packing solve keeps the pair,
        # which carries strictly more information for the same bytes.
        verdict = self._verdict(
            (RiskTier.HIGH, "rollback of core-db will break payment-api"),
            (RiskTier.MEDIUM, "the pool root cause is unconfirmed " + "y" * 90),
            (RiskTier.MEDIUM, "this reverses an earlier hold on core-db"),
            (RiskTier.MEDIUM, "the error rate reading is unclear"),
        )
        # 228 bytes leaves room for either the verbose finding alone or both
        # concise ones -- exactly the trade greedy-by-severity gets wrong.
        text = build_intervention_text(verdict, GovernorAction.WARN, max_bytes=228)

        self.assertLessEqual(len(text.encode("utf-8")), 228)
        self.assertNotIn("yyyy", text, "the verbose finding crowded out two shorter ones")
        self.assertIn("reverses an earlier hold", text)
        self.assertIn("error rate reading is unclear", text)

    def test_the_most_severe_finding_is_never_dropped(self) -> None:
        # Its tier decides the intervention's tier, so trading it away would
        # downgrade a warning into a question.
        verdict = self._verdict(
            (RiskTier.HIGH, "rollback of core-db breaks payment-api and auth-service"),
            (RiskTier.MEDIUM, "minor a"),
            (RiskTier.MEDIUM, "minor b"),
            (RiskTier.MEDIUM, "minor c"),
        )
        text = build_intervention_text(verdict, GovernorAction.WARN, max_bytes=200)
        self.assertIn("breaks payment-api", text)

    def test_the_announced_count_matches_what_is_actually_said(self) -> None:
        verdict = self._verdict(
            (RiskTier.HIGH, "first"),
            (RiskTier.MEDIUM, "second"),
            (RiskTier.MEDIUM, "third"),
        )
        text = build_intervention_text(verdict, GovernorAction.WARN)
        self.assertIn("three issues", text)
        self.assertEqual(sum(word in text for word in ("First.", "Second.", "Third.")), 3)

    def test_the_budget_accounts_for_the_widest_spoken_count_word(self) -> None:
        # The frame grows with the number of findings: "two issues" becomes
        # "three issues", two bytes more. Budgeting against the one-finding
        # frame lets an optimal pack land over the cap. Sweeping the budget
        # byte by byte walks the packing across every boundary where the
        # chosen set -- and so the count word -- changes.
        verdict = self._verdict(
            (RiskTier.HIGH, "rollback of core-db will break payment-api"),
            (RiskTier.MEDIUM, "this reverses an earlier hold on core-db"),
            (RiskTier.MEDIUM, "the error rate reading is unclear"),
            (RiskTier.MEDIUM, "the pool root cause is still unconfirmed"),
        )
        for budget in range(100, 320):
            with self.subTest(budget=budget):
                try:
                    text = build_intervention_text(
                        verdict, GovernorAction.WARN, max_bytes=budget
                    )
                except SpeechTooLongError:
                    continue  # too small for even the lead finding
                self.assertLessEqual(len(text.encode("utf-8")), budget)

    def test_packing_never_exceeds_the_budget_under_stress(self) -> None:
        for count in range(1, 9):
            with self.subTest(findings=count):
                verdict = self._verdict(
                    (RiskTier.HIGH, "lead finding with a realistic length to it"),
                    *[(RiskTier.MEDIUM, f"finding {i} " + "z" * (20 * i)) for i in range(1, count)],
                )
                text = build_intervention_text(verdict, GovernorAction.WARN)
                self.assertLessEqual(len(text.encode("utf-8")), SPEAK_MAX_BYTES)


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
