"""
Confirmation attribution tests.

This is the one place in AEGIS where a human utterance changes the
authorisation state of a consequential action, so it is tested from the angle
that matters: not "does a yes work" but "can a yes ever land on the wrong
thing". The policy's job is to refuse when it cannot be sure, and most of
these tests assert a refusal.

Two layers are covered. The pure policy is exercised directly, because it is
a decision procedure and deserves exhaustive cases without a database. Then
the pipeline tests prove the refusal actually reaches the ledger -- a policy
that returns AMBIGUOUS while the orchestrator resolves anyway would pass
every test in the first half of this file.
"""

from __future__ import annotations

import unittest

from backend.common.enums import (
    ActionKind,
    ClaimType,
    ProposedActionStatus,
    RiskFindingCode,
    SourceModality,
)
from backend.common.models import ExtractedClaim
from backend.pipeline.resolution import (
    ResolutionDecision,
    ResolutionOutcome,
    clarification_message,
    describe_candidates,
    select_action_to_resolve,
)
from backend.tests.support import at, make_action

WINDOW = 120.0


def reply(
    *,
    when: float = 20,
    uid: str = "1002",
    target_ref: str = None,
    action_kind: ActionKind = None,
    kind: ClaimType = ClaimType.CONFIRMATION,
    text: str = "yeah, go ahead",
) -> ExtractedClaim:
    return ExtractedClaim(
        type=kind,
        text=text,
        speaker_uid=uid,
        timestamp=at(when),
        source_turn_id="turn-reply",
        target_ref=target_ref,
        action_kind=action_kind,
        source_modality=SourceModality.VOICE,
    )


def decide(pending, claim, *, window: float = WINDOW, raised=None) -> ResolutionDecision:
    return select_action_to_resolve(
        pending, claim, window_seconds=window, last_raised_at=raised
    )


class UnambiguousRepliesResolveTests(unittest.TestCase):
    """The policy has to be strict without being useless: the ordinary cases
    a demo depends on must still work."""

    def test_one_open_action_and_a_bare_yes(self) -> None:
        action = make_action(when=10)
        outcome = decide([action], reply(when=20))
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)
        self.assertEqual(outcome.action.claim_id, action.claim_id)

    def test_a_named_target_picks_its_action_out_of_several(self) -> None:
        core = make_action(when=10, target_ref="core-db")
        search = make_action(when=15, target_ref="search-index", action_kind=ActionKind.RESTART)
        outcome = decide([core, search], reply(when=20, target_ref="search-index"))
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)
        self.assertEqual(outcome.action.target_ref, "search-index")

    def test_a_named_target_beats_recency(self) -> None:
        # The old heuristic returned the most recent pending action whenever a
        # named match was not the last one proposed. Naming it must win.
        core = make_action(when=10, target_ref="core-db")
        search = make_action(when=99, target_ref="search-index", action_kind=ActionKind.RESTART)
        outcome = decide([core, search], reply(when=100, target_ref="core-db"))
        self.assertEqual(outcome.action.target_ref, "core-db")

    def test_action_kind_separates_two_actions_on_one_component(self) -> None:
        rollback = make_action(when=10, target_ref="core-db", action_kind=ActionKind.ROLLBACK)
        restart = make_action(when=12, target_ref="core-db", action_kind=ActionKind.RESTART)
        outcome = decide(
            [rollback, restart],
            reply(when=20, target_ref="core-db", action_kind=ActionKind.RESTART),
        )
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)
        self.assertIs(outcome.action.action_kind, ActionKind.RESTART)

    def test_a_named_action_resolves_however_old_it_is(self) -> None:
        # Naming the target *is* the disambiguation. Refusing an explicit
        # human instruction on age would be refusing a clear decision.
        action = make_action(when=10, target_ref="core-db")
        outcome = decide([action], reply(when=10_000, target_ref="core-db"))
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)

    def test_holds_and_overrides_use_the_same_policy(self) -> None:
        action = make_action(when=10)
        for kind in (ClaimType.HOLD, ClaimType.OVERRIDE, ClaimType.CONFIRMATION):
            with self.subTest(kind=kind):
                outcome = decide([action], reply(when=20, kind=kind, text="no, don't"))
                self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)


class AmbiguityIsRefusedTests(unittest.TestCase):
    """The failure this policy exists to prevent: a reply landing on an action
    the speaker was not talking about."""

    def test_a_bare_yes_with_two_open_actions_resolves_nothing(self) -> None:
        core = make_action(when=10, target_ref="core-db")
        search = make_action(when=15, target_ref="search-index", action_kind=ActionKind.RESTART)
        outcome = decide([core, search], reply(when=20))
        self.assertIs(outcome.outcome, ResolutionOutcome.AMBIGUOUS)
        self.assertIsNone(outcome.action)
        self.assertEqual(len(outcome.candidates), 2)

    def test_it_does_not_quietly_pick_the_most_recent(self) -> None:
        # Explicit regression on the previous heuristic, which returned
        # pending[-1] and logged a warning. A warning is not a safety rule.
        actions = [make_action(when=10 + index, target_ref=name)
                   for index, name in enumerate(("core-db", "search-index", "cache-layer"))]
        outcome = decide(actions, reply(when=30))
        self.assertIsNone(outcome.action)

    def test_two_actions_on_the_same_component_are_ambiguous_without_a_kind(self) -> None:
        rollback = make_action(when=10, target_ref="core-db", action_kind=ActionKind.ROLLBACK)
        restart = make_action(when=12, target_ref="core-db", action_kind=ActionKind.RESTART)
        outcome = decide([rollback, restart], reply(when=20, target_ref="core-db"))
        self.assertIs(outcome.outcome, ResolutionOutcome.AMBIGUOUS)
        self.assertEqual(len(outcome.candidates), 2)

    def test_an_unmatched_kind_does_not_narrow_to_nothing(self) -> None:
        # If the stated kind matches neither open action, the reply is still
        # about *something* on that component -- so it stays ambiguous rather
        # than silently becoming "no such target".
        rollback = make_action(when=10, target_ref="core-db", action_kind=ActionKind.ROLLBACK)
        restart = make_action(when=12, target_ref="core-db", action_kind=ActionKind.RESTART)
        outcome = decide(
            [rollback, restart],
            reply(when=20, target_ref="core-db", action_kind=ActionKind.CONFIG_CHANGE),
        )
        self.assertIs(outcome.outcome, ResolutionOutcome.AMBIGUOUS)

    def test_ambiguity_asks_for_clarification(self) -> None:
        core = make_action(when=10, target_ref="core-db")
        search = make_action(when=15, target_ref="search-index", action_kind=ActionKind.RESTART)
        outcome = decide([core, search], reply(when=20))
        self.assertTrue(outcome.outcome.needs_clarification)
        message = clarification_message(outcome)
        self.assertIn("core-db", message)
        self.assertIn("search-index", message)
        self.assertTrue(message.rstrip().endswith("?"))


class TemporalRelevanceTests(unittest.TestCase):
    """An action stays pending forever, which is right. A bare "yeah" twenty
    minutes later is not a decision about it, which is also right."""

    def test_a_stale_bare_reply_does_not_resolve(self) -> None:
        action = make_action(when=10)
        outcome = decide([action], reply(when=10 + WINDOW + 1))
        self.assertIs(outcome.outcome, ResolutionOutcome.OUT_OF_WINDOW)
        self.assertIsNone(outcome.action)

    def test_the_boundary_itself_still_resolves(self) -> None:
        action = make_action(when=10)
        outcome = decide([action], reply(when=10 + WINDOW))
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)

    def test_aegis_raising_it_restarts_the_clock(self) -> None:
        # The most natural moment for a human to finally answer is right after
        # AEGIS re-opens the subject. Measuring only from the proposal would
        # time out exactly that answer.
        action = make_action(when=10)
        raised = {action.claim_id: at(500)}
        outcome = decide([action], reply(when=560), raised=raised)
        self.assertIs(outcome.outcome, ResolutionOutcome.RESOLVED)

    def test_a_raise_older_than_the_window_does_not_rescue_it(self) -> None:
        action = make_action(when=10)
        raised = {action.claim_id: at(100)}
        outcome = decide([action], reply(when=100 + WINDOW + 1), raised=raised)
        self.assertIs(outcome.outcome, ResolutionOutcome.OUT_OF_WINDOW)

    def test_a_stale_single_action_is_asked_about_not_ignored(self) -> None:
        action = make_action(when=10, target_ref="core-db")
        outcome = decide([action], reply(when=10_000))
        self.assertTrue(outcome.outcome.needs_clarification)
        self.assertIn("core-db", clarification_message(outcome))


class NothingToResolveTests(unittest.TestCase):
    def test_a_reply_with_nothing_pending(self) -> None:
        outcome = decide([], reply(when=20))
        self.assertIs(outcome.outcome, ResolutionOutcome.NOTHING_PENDING)
        self.assertFalse(outcome.outcome.needs_clarification)

    def test_a_reply_naming_a_component_with_nothing_open_on_it(self) -> None:
        action = make_action(when=10, target_ref="core-db")
        outcome = decide([action], reply(when=20, target_ref="payment-api"))
        self.assertIs(outcome.outcome, ResolutionOutcome.NO_SUCH_TARGET)
        self.assertIsNone(outcome.action)
        # Nothing to ask about: they named something that was never proposed.
        self.assertFalse(outcome.outcome.needs_clarification)


class DecisionShapeTests(unittest.TestCase):
    """The outcome and the action cannot disagree. A caller that reads
    ``.action`` without checking the outcome must not be able to authorise
    something the policy refused."""

    def test_a_refusal_cannot_carry_an_action(self) -> None:
        with self.assertRaises(ValueError):
            ResolutionDecision(
                outcome=ResolutionOutcome.AMBIGUOUS,
                action=make_action(),
            )

    def test_a_resolution_must_carry_one(self) -> None:
        with self.assertRaises(ValueError):
            ResolutionDecision(outcome=ResolutionOutcome.RESOLVED)

    def test_candidates_are_described_in_human_terms(self) -> None:
        described = describe_candidates(
            [
                make_action(target_ref="core-db", action_kind=ActionKind.ROLLBACK),
                make_action(target_ref="search-index", action_kind=ActionKind.RESTART),
            ]
        )
        self.assertIn("core-db rollback", described)
        self.assertIn("search-index restart", described)

    def test_a_long_candidate_list_is_summarised_rather_than_recited(self) -> None:
        described = describe_candidates(
            [make_action(target_ref=f"svc-{index}") for index in range(6)]
        )
        self.assertIn("something else still open", described)


class ResolutionThroughThePipelineTests(unittest.TestCase):
    """The policy refusing is only half of it; the ledger has to agree."""

    def setUp(self) -> None:
        from backend.common.clock import ManualClock
        from backend.common.models import TranscriptEvent
        from backend.tests.support import T0
        from backend.tests.test_pipeline import runtime

        self._TranscriptEvent = TranscriptEvent
        self.clock = ManualClock(start=T0)
        self.rt = runtime(self.clock)
        self.addCleanup(self.rt.close)
        self._turn = 0

    def say(self, text: str, *, uid: str = "1001", advance: float = 5.0):
        self._turn += 1
        self.clock.advance(advance)
        return self.rt.pipeline.handle_transcript(
            self._TranscriptEvent(
                uid=uid,
                turn_id=f"turn-{self._turn}",
                role="human",
                text=text,
                final=True,
                timestamp=self.clock.now(),
                source_modality=SourceModality.VOICE,
            )
        )

    def _two_open_actions(self) -> None:
        self.say("Let's roll back search-index.")
        self.say("Let's restart notification-service.", advance=50)

    def statuses(self) -> list[str]:
        view = self.rt.store.snapshot(captured_at=self.clock.now())
        return [action.status.value for action in view.proposed_actions]

    def test_an_ambiguous_yes_authorises_nothing(self) -> None:
        self._two_open_actions()
        self.say("Yeah, go ahead.", uid="1002", advance=50)
        self.assertEqual(self.statuses().count("confirmed"), 0)
        self.assertEqual(len(self.rt.store.pending_actions()), 2)

    def test_an_ambiguous_yes_makes_aegis_ask_which(self) -> None:
        self._two_open_actions()
        result = self.say("Yeah, go ahead.", uid="1002", advance=50)
        self.assertTrue(result.spoken, "AEGIS silently swallowed an ambiguous decision")
        spoken = result.spoken[0]
        self.assertIn("search-index", spoken)
        self.assertIn("notification-service", spoken)
        self.assertLessEqual(len(spoken.encode("utf-8")), 512)

    def test_naming_the_target_then_resolves_it(self) -> None:
        self._two_open_actions()
        self.say("Yeah, go ahead.", uid="1002", advance=50)
        self.say("Yes, roll back search-index.", uid="1002", advance=50)

        actions = self.rt.store.snapshot(captured_at=self.clock.now()).proposed_actions
        by_target = {action.target_ref: action for action in actions}
        self.assertIs(by_target["search-index"].status, ProposedActionStatus.CONFIRMED)
        self.assertEqual(by_target["search-index"].resolved_by_uid, "1002")
        self.assertIs(by_target["notification-service"].status, ProposedActionStatus.PENDING)

    def test_the_refusal_is_counted_and_the_resolution_is_too(self) -> None:
        self._two_open_actions()
        self.say("Yeah, go ahead.", uid="1002", advance=50)
        self.say("Yes, roll back search-index.", uid="1002", advance=50)
        counters = self.rt.pipeline.metrics.snapshot()["counters"]
        self.assertGreaterEqual(counters.get("resolutions_refused_ambiguous", 0), 1)
        self.assertGreaterEqual(counters.get("resolutions_applied", 0), 1)

    def test_the_clarifying_question_is_recorded_as_an_intervention(self) -> None:
        self._two_open_actions()
        self.say("Yeah, go ahead.", uid="1002", advance=50)
        codes = [
            code
            for record in self.rt.store.interventions()
            for code in record.codes
        ]
        self.assertIn(RiskFindingCode.AMBIGUOUS_CONFIRMATION, codes)

    def test_the_question_is_never_asked_late(self) -> None:
        # Rate limited: the window is shut, and "which one did you mean?"
        # forty-five seconds after the fact is worse than silence. Nothing is
        # authorised either way, which is the point.
        self.say("Let's roll back search-index.")
        self.say("Let's rollback Core.", advance=5)  # HIGH: takes the window
        self.say("Yeah, go ahead.", uid="1002", advance=5)
        self.assertEqual(len(self.rt.store.pending_actions()), 2)
        self.assertEqual(self.rt.governor.queue_depth, 0, "a stale question was queued")

    def test_a_second_voice_agreeing_does_not_re_open_the_action(self) -> None:
        # Found by the concurrency fuzz: "yes, roll back search-index" is
        # indistinguishable from a fresh proposal once the action it answers
        # is already resolved, so a second person agreeing produced a brand
        # new pending rollback for something the room had just decided.
        self.say("Let's roll back search-index.")
        self.say("Yes, roll back search-index.", uid="1002", advance=10)
        self.say("Yes, roll back search-index.", uid="1003", advance=2)

        actions = self.rt.store.snapshot(captured_at=self.clock.now()).proposed_actions
        self.assertEqual(len(actions), 1, "an echo re-opened a decided action")
        self.assertIs(actions[0].status, ProposedActionStatus.CONFIRMED)

    def test_a_genuine_re_proposal_later_is_still_a_proposal(self) -> None:
        # The echo guard must not swallow "that didn't help, let's do it
        # again" -- it is scoped to the window in which a reply is still
        # about the same moment.
        self.say("Let's roll back search-index.")
        self.say("Yes, roll back search-index.", uid="1002", advance=10)
        self.say("Let's roll back search-index.", uid="1001", advance=300)

        actions = self.rt.store.snapshot(captured_at=self.clock.now()).proposed_actions
        self.assertEqual(len(actions), 2)
        self.assertEqual(
            sorted(a.status.value for a in actions), ["confirmed", "pending"]
        )

    def test_a_different_action_on_the_same_target_is_not_an_echo(self) -> None:
        self.say("Let's roll back search-index.")
        self.say("Yes, roll back search-index.", uid="1002", advance=10)
        self.say("Let's restart search-index.", uid="1001", advance=5)
        actions = self.rt.store.snapshot(captured_at=self.clock.now()).proposed_actions
        self.assertEqual(len(actions), 2)

    def test_a_single_open_action_still_takes_a_plain_yes(self) -> None:
        # The strictness must not break the ordinary path the demo relies on.
        self.say("Let's roll back search-index.")
        self.say("Yes, go ahead.", uid="1002", advance=10)
        self.assertEqual(self.statuses().count("confirmed"), 1)

    def test_a_yes_long_after_the_only_proposal_does_not_authorise_it(self) -> None:
        self.say("Let's roll back search-index.")
        self.say("Anyway, the dashboards look quiet.", advance=60)
        result = self.say("yes, go ahead", uid="1002", advance=600)
        self.assertEqual(self.statuses().count("confirmed"), 0)
        # And it is asked about rather than silently dropped -- the human
        # clearly decided something; AEGIS just cannot tell what.
        self.assertTrue(result.spoken)
        self.assertIn("search-index", result.spoken[0])

    def test_a_bare_yes_is_an_answer_when_something_is_pending(self) -> None:
        # The extraction fast path treats "yeah" as filler, which it is --
        # right up until AEGIS has asked a question. An optimisation that can
        # swallow a human's answer is not an optimisation.
        self.say("Let's roll back search-index.")
        self.say("yeah", uid="1002", advance=10)
        self.assertEqual(self.statuses().count("confirmed"), 1)

    def test_an_acknowledgement_is_not_an_answer(self) -> None:
        # "Okay" may mean "I agree" or may mean "I heard you". A system that
        # cannot tell those apart must not treat either as authorisation.
        for token in ("okay", "ok", "sure", "right", "mm hmm"):
            with self.subTest(token=token):
                fresh = ResolutionThroughThePipelineTests("test_an_acknowledgement_is_not_an_answer")
                fresh.setUp()
                try:
                    fresh.say("Let's roll back search-index.")
                    fresh.say(token, uid="1002", advance=10)
                    self.assertEqual(
                        fresh.statuses().count("confirmed"),
                        0,
                        f"{token!r} authorised a rollback",
                    )
                finally:
                    fresh.doCleanups()


if __name__ == "__main__":
    unittest.main()
