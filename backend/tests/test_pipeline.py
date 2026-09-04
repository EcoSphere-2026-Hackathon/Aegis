"""
End-to-end pipeline tests, including the golden demo.

This is the file that answers "does the product work". It drives the whole
loop -- utterance in, intervention out, human decision applied -- with no
network and no live voice, exactly as the build order requires the text-only
pipeline to be provable before Agora is wired in.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import timedelta

from backend.common.clock import ManualClock
from backend.common.config import AppConfig, GovernorConfig, load_config
from backend.common.enums import (
    ClaimType,
    DecisionStance,
    EvidenceSource,
    EvidenceSourceType,
    ExtractionCertainty,
    HypothesisStatus,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
    SourceModality,
)
from backend.common.errors import InterventionError
from backend.common.models import Evidence, TranscriptEvent
from backend.pipeline.factory import build_runtime
from backend.pipeline.sinks import FailingSink, RecordingSink
from backend.tests.support import T0


def runtime(clock: ManualClock, *, sink=None, rate_limit: float = 45.0):
    import os
    config = load_config(env=os.environ, dotenv_path=None, project_root=None)
    config = AppConfig(
        agora=config.agora,
        llm=config.llm,
        governor=GovernorConfig(rate_limit_seconds=rate_limit),
        pipeline=config.pipeline,
        api=config.api,
        database_path=config.database_path,
        incident_id="test-incident",
        log_level="CRITICAL",
    )
    return build_runtime(config, clock=clock, sink=sink or RecordingSink(clock=clock),
                         database_path=":memory:")


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(start=T0)
        self.rt = runtime(self.clock)
        self.addCleanup(self.rt.close)
        self._turn = 0

    def say(self, text: str, *, uid: str = "1001", advance: float = 5.0,
            modality: SourceModality = SourceModality.VOICE):
        self._turn += 1
        self.clock.advance(advance)
        event = TranscriptEvent(
            uid=uid,
            turn_id=f"turn-{self._turn}",
            role="human",
            text=text,
            final=True,
            timestamp=self.clock.now(),
            source_modality=modality,
        )
        return self.rt.pipeline.handle_transcript(event)

    @property
    def spoken(self) -> tuple[str, ...]:
        return self.rt.sink.lines


class GoldenDemoTests(PipelineTestCase):
    """SSOT §20, beats 1-9, driven through the real pipeline."""

    def test_the_full_golden_demo_runs_end_to_end(self) -> None:
        # Beat 1 -- ordinary incident chatter. AEGIS stays silent.
        beat1 = self.say("Payments are throwing 500s, seeing timeouts.", uid="1001")
        self.assertIn(ClaimType.FACT, [c.type for c in beat1.claims])
        self.assertFalse(beat1.spoke, "AEGIS spoke during ordinary conversation")

        # Beat 2/3 -- a figure recited from impression, contradicted by
        # telemetry. First killer moment.
        beat2 = self.say("Pool utilization looks fine, like 40%.", uid="1002")
        self.assertTrue(beat2.spoke, "AEGIS failed to ground the claim against telemetry")
        first_intervention = beat2.spoken[0]
        self.assertIn("91", first_intervention)
        self.assertIn("40", first_intervention)
        self.assertIn(RiskFindingCode.EVIDENCE_CONTRADICTION, beat2.verdicts[0].codes)

        # The contradicted theory is now stale, not merely noted.
        hypotheses = self.rt.store.snapshot(captured_at=self.clock.now()).hypotheses
        pool_theory = next(h for h in hypotheses if h.metric_ref == "pool_utilization")
        self.assertIs(pool_theory.status, HypothesisStatus.STALE)

        # Rate limit must have closed behind that intervention.
        self.assertFalse(self.rt.governor.window_is_open())

        # Beat 4/5/6 -- the compound catch. Wait out the window first, as the
        # demo script does.
        beat4 = self.say(
            "Okay, fine, it is the pool then. Let's rollback Core to the last version.",
            uid="1001",
            advance=50.0,
        )
        types = [c.type for c in beat4.claims]
        self.assertIn(ClaimType.PROPOSED_ACTION, types)
        self.assertTrue(beat4.spoke, "AEGIS failed to catch the rollback")

        second_intervention = beat4.spoken[0]
        verdict = beat4.verdicts[-1]
        self.assertEqual(verdict.risk_tier, RiskTier.HIGH)
        self.assertIn(RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK, verdict.codes)
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, verdict.codes)
        self.assertIn("payment-api", second_intervention)
        self.assertIn("auth-service", second_intervention)
        self.assertIn("v17", second_intervention)
        self.assertIn("v2.3", second_intervention)
        self.assertIn("two issues", second_intervention)
        self.assertLessEqual(len(second_intervention.encode("utf-8")), 512)

        # The action is pending and stays pending until a human speaks.
        pending = self.rt.store.pending_actions()
        self.assertEqual(len(pending), 1)

        # Beat 7 -- a human holds it. The boundary is respected.
        beat7 = self.say("Hold — don't rollback, let's check the pool metrics properly first.",
                          uid="1002", advance=5.0)
        self.assertEqual(len(beat7.resolved_action_ids), 1)
        resolved = self.rt.store.get_proposed_action(beat7.resolved_action_ids[0])
        self.assertIs(resolved.status, ProposedActionStatus.HELD)
        self.assertEqual(resolved.resolved_by_uid, "1002")
        self.assertEqual(self.rt.store.pending_actions(), ())

        # Beat 8 -- on-demand spoken status.
        beat8 = self.say("AEGIS, status?", uid="1001", advance=50.0)
        self.assertTrue(beat8.spoke)
        summary = beat8.spoken[0]
        self.assertIn("Status.", summary)
        self.assertIn("unconfirmed", summary)

        # Beat 9 -- the closing artefact is built from accumulated state.
        view = self.rt.store.incident_view(captured_at=self.clock.now())
        self.assertGreaterEqual(len(view.facts), 1)
        self.assertGreaterEqual(len(view.hypotheses), 2)
        self.assertGreaterEqual(len(view.proposed_actions), 1)
        self.assertGreaterEqual(len(view.evidence), 1)
        self.assertGreaterEqual(len(view.interventions), 2)
        self.assertEqual(
            [entry.occurred_at for entry in view.timeline],
            sorted(entry.occurred_at for entry in view.timeline),
        )

    def test_both_killer_moments_are_distinct_reasoning_paths(self) -> None:
        # The pitch rests on these being two different capabilities rather
        # than one trick shown twice.
        self.say("Pool utilization looks fine, like 40%.", uid="1002")
        first_codes = set(self.rt.store.interventions()[0].codes)

        self.say("Okay, it is the pool then. Let's rollback Core.", uid="1001", advance=50.0)
        second_codes = set(self.rt.store.interventions()[-1].codes)

        self.assertIn(RiskFindingCode.EVIDENCE_CONTRADICTION, first_codes)
        self.assertIn(RiskFindingCode.BLAST_RADIUS_SCHEMA_BREAK, second_codes)
        self.assertNotEqual(first_codes, second_codes)


class SilenceTests(PipelineTestCase):
    def test_small_talk_produces_no_intervention(self) -> None:
        for line in ["Morning.", "okay", "yeah", "Can you hear me alright?"]:
            result = self.say(line)
            self.assertFalse(result.spoke, f"AEGIS spoke over: {line!r}")

    def test_a_low_risk_action_is_not_interrupted(self) -> None:
        result = self.say("Let's restart search-index, it's just the indexer.")
        self.assertFalse(result.spoke)

    def test_interim_transcripts_are_ignored(self) -> None:
        event = TranscriptEvent(uid="1001", turn_id="t-interim", role="human",
                                text="let's roll back core", final=False,
                                timestamp=self.clock.now())
        result = self.rt.pipeline.handle_transcript(event)
        self.assertEqual(result.claims, ())
        self.assertEqual(self.rt.store.pending_actions(), ())

    def test_agent_turns_are_not_re_ingested(self) -> None:
        event = TranscriptEvent(uid="9000", turn_id="t-agent", role="agent",
                                text="Hold — rolling back core-db will break payment-api.",
                                final=True, timestamp=self.clock.now())
        result = self.rt.pipeline.handle_transcript(event)
        self.assertEqual(result.claims, ())


class AuthorisationBoundaryTests(PipelineTestCase):
    """Quality Standard §4 red line #1: nothing is authorised without an
    explicit human resolution."""

    def _propose(self) -> str:
        self.say("It might be the pool.", uid="1002")
        self.say("Let's rollback Core.", uid="1001", advance=50.0)
        pending = self.rt.store.pending_actions()
        self.assertEqual(len(pending), 1)
        return pending[0].claim_id

    def test_silence_never_authorises(self) -> None:
        claim_id = self._propose()
        for _ in range(10):
            self.say("...", advance=60.0)
        self.assertIs(
            self.rt.store.get_proposed_action(claim_id).status, ProposedActionStatus.PENDING
        )

    def test_ambiguous_reply_does_not_confirm(self) -> None:
        claim_id = self._propose()
        self.say("uh, I guess maybe", uid="1002", advance=5.0)
        self.assertIs(
            self.rt.store.get_proposed_action(claim_id).status, ProposedActionStatus.PENDING
        )

    def test_explicit_confirmation_authorises_and_records_the_human(self) -> None:
        claim_id = self._propose()
        self.say("Yes, go ahead.", uid="1002", advance=5.0)
        action = self.rt.store.get_proposed_action(claim_id)
        self.assertIs(action.status, ProposedActionStatus.CONFIRMED)
        self.assertEqual(action.resolved_by_uid, "1002")

    def test_an_override_declines_it(self) -> None:
        claim_id = self._propose()
        self.say("No, don't do that.", uid="1002", advance=5.0)
        self.assertIs(
            self.rt.store.get_proposed_action(claim_id).status, ProposedActionStatus.DECLINED
        )

    def test_a_resolution_with_nothing_pending_is_harmless(self) -> None:
        result = self.say("Yes, go ahead.", uid="1002")
        self.assertEqual(result.resolved_action_ids, ())

    def test_aegis_never_executes_anything(self) -> None:
        # There is no execution surface at all: the only outbound effect the
        # pipeline can produce is speech.
        claim_id = self._propose()
        self.say("Yes, go ahead.", uid="1002", advance=5.0)
        action = self.rt.store.get_proposed_action(claim_id)
        self.assertIs(action.status, ProposedActionStatus.CONFIRMED)
        # Confirmed means "a human authorised it", not "AEGIS did it".
        for line in self.spoken:
            self.assertNotIn("executing", line.lower())
            self.assertNotIn("i have rolled", line.lower())


class RateLimitInteractionTests(PipelineTestCase):
    def test_a_second_risk_inside_the_window_is_not_spoken(self) -> None:
        self.say("Pool utilization looks fine, like 40%.", uid="1002")
        self.assertEqual(len(self.spoken), 1)

        before = len(self.spoken)
        self.say("Let's rollback Core.", uid="1001", advance=5.0)
        self.assertEqual(len(self.spoken), before, "rate limit was bypassed")

    def test_a_queued_warning_is_re_evaluated_when_the_window_reopens(self) -> None:
        self.say("Pool utilization looks fine, like 40%.", uid="1002")
        self.say("Let's rollback Core.", uid="1001", advance=5.0)  # queued
        self.assertGreaterEqual(self.rt.governor.queue_depth, 1)

        before = len(self.spoken)
        self.say("Still looking at it.", uid="1001", advance=60.0)
        self.assertGreater(len(self.spoken), before, "queued warning was never delivered")
        self.assertIn("payment-api", self.spoken[-1])

    def test_a_queued_warning_is_dropped_if_humans_resolved_it_first(self) -> None:
        self.say("Pool utilization looks fine, like 40%.", uid="1002")
        self.say("Let's rollback Core.", uid="1001", advance=5.0)  # queued
        self.say("Hold on that.", uid="1002", advance=5.0)  # resolved by a human

        before = len(self.spoken)
        self.say("Anything else?", uid="1001", advance=60.0)
        self.assertEqual(len(self.spoken), before,
                         "AEGIS argued with a decision the humans had already made")


class DeliveryFailureTests(unittest.TestCase):
    def test_a_failed_delivery_returns_the_rate_limit_window(self) -> None:
        clock = ManualClock(start=T0)
        rt = runtime(clock, sink=FailingSink())
        self.addCleanup(rt.close)

        event = TranscriptEvent(uid="1002", turn_id="t1", role="human",
                                text="Pool utilization looks fine, like 40%.",
                                final=True, timestamp=clock.now())
        rt.pipeline.handle_transcript(event)

        self.assertTrue(rt.governor.window_is_open(),
                        "a failed speak call silenced AEGIS for the whole window")
        records = rt.store.interventions()
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0].delivery_error)


class EvidenceIngestionTests(PipelineTestCase):
    """The multimodal path reuses the same reasoning and the same voice.

    Each test starts from a theory telemetry *agrees* with, so the claim is
    still live when the screenshot arrives. Otherwise automatic grounding
    would already have settled it and the submitted evidence would have
    nothing left to contradict -- which is correct behaviour, but tests
    nothing about the multimodal path.
    """

    def test_submitted_evidence_contradicting_a_theory_triggers_the_same_flow(self) -> None:
        self.rt.telemetry.set_value("error_rate", 2.0)
        self.say("Error rate is only about 2%.", uid="1002")
        before = len(self.spoken)

        self.clock.advance(50)
        decision = self.rt.pipeline.ingest_evidence(
            Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name="error_rate",
                value=12.4,
                unit="%",
                extraction_certainty=ExtractionCertainty.HIGH,
                uploader_uid="1001",
                timestamp=self.clock.now(),
            )
        )
        self.assertIsNotNone(decision)
        self.assertGreater(len(self.spoken), before)
        self.assertIn("screenshot", self.spoken[-1].lower())

    def test_low_certainty_evidence_asks_rather_than_warns(self) -> None:
        self.rt.telemetry.set_value("error_rate", 2.0)
        self.say("Error rate is only about 2%.", uid="1002")
        self.clock.advance(50)
        decision = self.rt.pipeline.ingest_evidence(
            Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name="error_rate",
                value=12.4,
                unit="%",
                extraction_certainty=ExtractionCertainty.LOW,
                uploader_uid="1001",
                timestamp=self.clock.now(),
            )
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.verdict.risk_tier, RiskTier.MEDIUM)
        self.assertTrue(self.spoken[-1].startswith("Quick check"))

    def test_agreeing_evidence_says_nothing(self) -> None:
        self.say("Error rate is around 12%.", uid="1002")
        before = len(self.spoken)
        self.clock.advance(50)
        self.rt.pipeline.ingest_evidence(
            Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name="error_rate",
                value=12.4,
                unit="%",
                uploader_uid="1001",
                timestamp=self.clock.now(),
            )
        )
        self.assertEqual(len(self.spoken), before)


class JustificationRetractionTests(PipelineTestCase):
    """The reasoning property: retracting a belief re-opens what rested on it.

    Every proposed action records the theory that justified it, so the store
    holds a justification graph. Each check on its own is stateless; what
    makes this a reasoning system is that invalidating a node re-examines its
    dependents instead of leaving a stale conclusion standing.
    """

    def _propose_on_a_live_theory(self) -> None:
        # Telemetry agrees, so the theory stands and the action built on it
        # is genuinely low risk. AEGIS must be silent here -- if it warns now,
        # the later escalation proves nothing.
        self.rt.telemetry.set_value("error_rate", 12.0)
        self.say("Error rate is around 12%, the retry storm is the cause.", uid="1002")
        # search-index is a leaf: rolling it back breaks nothing downstream,
        # so the *only* thing that could make this risky is the theory behind
        # it. That isolates the property under test.
        self.action = self.say("Let's roll back search-index then.", uid="1001")

    def test_a_pending_action_is_re_raised_when_its_justification_collapses(self) -> None:
        self._propose_on_a_live_theory()
        self.assertFalse(self.action.spoke, "AEGIS warned before the theory was in doubt")
        before = len(self.spoken)

        # Reality moves. The theory dies -- and the rollback resting on it is
        # still pending, still carrying a verdict computed against a belief
        # nobody holds any more.
        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        result = self.say("Error rate is down to 0.3% now.", uid="1002")

        self.assertGreater(len(self.spoken), before, "the collapse passed unmentioned")
        spoken = self.spoken[-1].lower()
        self.assertTrue(
            "root cause" in spoken or "contradicted" in spoken or "unconfirmed" in spoken,
            spoken,
        )
        self.assertTrue(result.spoke, "the escalation was invisible to the turn result")

    def test_the_re_evaluation_is_recorded_on_the_action_itself(self) -> None:
        self._propose_on_a_live_theory()
        action_id = next(
            claim.claim_id
            for claim in self.action.claims
            if claim.type is ClaimType.PROPOSED_ACTION
        )
        first = self.rt.store.get_proposed_action(action_id)
        self.assertIsNotNone(first.risk_verdict)
        self.assertIs(first.risk_verdict.risk_tier, RiskTier.LOW)

        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        self.say("Error rate is down to 0.3% now.", uid="1002")

        after = self.rt.store.get_proposed_action(action_id)
        self.assertIs(after.status, ProposedActionStatus.PENDING, "AEGIS altered the action")
        self.assertGreater(
            after.risk_verdict.risk_tier.rank,
            first.risk_verdict.risk_tier.rank,
            "the stored verdict still reflects the collapsed theory",
        )
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, after.risk_verdict.codes)

    def test_retraction_propagates_when_reality_arrives_as_evidence(self) -> None:
        """The same collapse, delivered through the multimodal door.

        A screenshot reading and a telemetry push retract a belief exactly as
        a spoken correction does, so everything resting on that belief has to
        be re-examined the same way. This propagation was missing on the
        evidence path: the theory went stale, and the rollback built on it
        kept the LOW verdict computed against a belief nobody held any more --
        the precise failure the justification graph exists to prevent,
        reachable through /api/evidence.
        """
        self._propose_on_a_live_theory()
        action_id = next(
            claim.claim_id
            for claim in self.action.claims
            if claim.type is ClaimType.PROPOSED_ACTION
        )
        before = self.rt.store.get_proposed_action(action_id)
        self.assertIs(before.risk_verdict.risk_tier, RiskTier.LOW)

        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        self.rt.pipeline.ingest_evidence(
            Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name="error_rate",
                value=0.3,
                unit="%",
                extraction_certainty=ExtractionCertainty.HIGH,
                uploader_uid="1001",
                timestamp=self.clock.now(),
            )
        )

        after = self.rt.store.get_proposed_action(action_id)
        self.assertIs(after.status, ProposedActionStatus.PENDING, "AEGIS altered the action")
        self.assertGreater(
            after.risk_verdict.risk_tier.rank,
            before.risk_verdict.risk_tier.rank,
            "the stored verdict still reflects the collapsed theory",
        )
        self.assertIn(RiskFindingCode.STALE_JUSTIFICATION, after.risk_verdict.codes)

    def test_a_resolved_action_is_not_dragged_back_up(self) -> None:
        # Humans already decided. Re-litigating it because a number moved
        # would be AEGIS arguing with a decision that has been made.
        self._propose_on_a_live_theory()
        self.say("Yes, go ahead with the search-index rollback.", uid="1001")
        before = len(self.spoken)

        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        self.say("Error rate is down to 0.3% now.", uid="1002")

        self.assertEqual(
            self.rt.pipeline.metrics.snapshot()["counters"].get("reevaluations_escalated", 0),
            0,
        )
        self.assertEqual(len(self.spoken), before)

    def test_re_evaluation_does_not_cascade(self) -> None:
        # Re-evaluating an action yields a verdict and nothing else. If it
        # could touch a hypothesis it could re-trigger itself, and one
        # telemetry reading could turn into an unbounded interrupt storm.
        self._propose_on_a_live_theory()
        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        before = len(self.spoken)
        self.say("Error rate is down to 0.3% now.", uid="1002")

        self.assertLessEqual(
            len(self.spoken) - before, 1, "one collapse produced more than one interruption"
        )

    def test_an_unjustified_action_has_nothing_to_re_open(self) -> None:
        self.say("Let's restart notification-service.", uid="1001")
        self.clock.advance(60)
        self.rt.telemetry.set_value("error_rate", 0.3)
        before = len(self.spoken)
        self.say("Error rate is down to 0.3% now.", uid="1002")
        self.assertEqual(len(self.spoken), before)


class TextModalityTests(PipelineTestCase):
    def test_typed_text_is_treated_exactly_like_speech(self) -> None:
        result = self.say("Pool utilization looks fine, like 40%.", uid="1002",
                          modality=SourceModality.TEXT)
        self.assertTrue(result.spoke)
        claim = next(c for c in result.claims if c.type is ClaimType.HYPOTHESIS)
        self.assertIs(claim.source_modality, SourceModality.TEXT)


class DecisionLedgerTests(PipelineTestCase):
    """A human decision belongs in the ledger whatever utterance carried it."""

    def test_a_hold_spoken_as_a_reply_is_recorded_as_a_decision(self) -> None:
        # It usually arrives this way -- as an answer to a pending action,
        # not as a standalone announcement. A resolution that left no
        # decision behind gave the reversal check nothing to read.
        self.say("Let's restart search-index.")
        self.say("No, hold off on that.", uid="1002")
        decisions = self.rt.store.snapshot(captured_at=self.clock.now()).decisions
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].target_ref, "search-index")
        self.assertIs(decisions[0].stance, DecisionStance.HOLD)
        self.assertEqual(decisions[0].speaker_uid, "1002")

    def test_reversing_that_hold_is_caught_on_a_low_risk_target(self) -> None:
        # The point of the fix: on a target with no blast radius, nothing
        # else would have spoken, so this reversal was silent.
        self.say("Let's restart search-index.")
        self.say("No, hold off on that.", uid="1002")
        result = self.say("Let's restart search-index.", advance=25)
        self.assertIn(RiskFindingCode.DECISION_REVERSAL, result.verdicts[0].codes)
        self.assertTrue(result.spoke)

    def test_a_confirmation_records_a_proceed_decision(self) -> None:
        # Which is what stops AEGIS calling a later agreement a reversal.
        self.say("Let's restart search-index.")
        self.say("Yes, restart search-index.", uid="1002")
        decisions = self.rt.store.snapshot(captured_at=self.clock.now()).decisions
        self.assertIs(decisions[0].stance, DecisionStance.PROCEED)


class IncidentResetTests(unittest.TestCase):
    """The demo has to be runnable twice.

    The default store is a file, so without a reset the second run starts
    from the first one's pending actions, spent turn ids and -- worst -- the
    governor's closed window and already-said set, which produce a run in
    which AEGIS says nothing at all.
    """

    def _run_the_first_two_beats(self, rt, clock) -> tuple[str, ...]:
        for index, text in enumerate(
            (
                "Payments are throwing 500s, seeing timeouts.",
                "Pool utilization looks fine, like 40%.",
            )
        ):
            clock.advance(5)
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001", turn_id=f"beat-{index}", role="human", text=text,
                    final=True, timestamp=clock.now(),
                    source_modality=SourceModality.VOICE,
                )
            )
        return rt.sink.lines

    def test_a_reset_empties_the_incident(self) -> None:
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)
        self._run_the_first_two_beats(rt, clock)
        self.assertTrue(rt.store.timeline())

        rt.reset()

        view = rt.store.snapshot(captured_at=clock.now())
        self.assertEqual(view.facts, ())
        self.assertEqual(view.hypotheses, ())
        self.assertEqual(view.proposed_actions, ())
        self.assertEqual(rt.store.timeline(), ())
        self.assertEqual(rt.store.interventions(), ())

    def test_the_second_run_is_not_silent(self) -> None:
        # The failure this exists to prevent, and the one that would be
        # hardest to diagnose live: a reset that cleared the database but
        # left the governor's window closed and its already-said set full.
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)
        first = self._run_the_first_two_beats(rt, clock)
        self.assertTrue(first, "the first run said nothing, so this proves nothing")

        rt.reset()
        rt.sink.clear()
        self.assertTrue(rt.governor.window_is_open(), "the window stayed closed")
        self.assertEqual(rt.governor.voiced_memory_size, 0)

        second = self._run_the_first_two_beats(rt, clock)
        self.assertEqual(
            list(second), list(first), "the second run did not reproduce the first"
        )

    def test_turn_ids_can_be_reused_after_a_reset(self) -> None:
        # Idempotency is keyed on turn id. Without clearing it, replaying the
        # same rehearsed script is indistinguishable from a duplicate feed.
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)
        self._run_the_first_two_beats(rt, clock)
        rt.reset()
        clock.advance(5)
        result = rt.pipeline.handle_transcript(
            TranscriptEvent(
                uid="1001", turn_id="beat-0", role="human",
                text="Payments are throwing 500s, seeing timeouts.", final=True,
                timestamp=clock.now(), source_modality=SourceModality.VOICE,
            )
        )
        self.assertFalse(result.duplicate)
        self.assertTrue(result.claims)

    def test_a_reset_forgets_moved_telemetry(self) -> None:
        clock = ManualClock(start=T0)
        rt = runtime(clock)
        self.addCleanup(rt.close)
        before = rt.telemetry.read("error_rate").value
        rt.telemetry.set_value("error_rate", 99.0)
        rt.reset()
        self.assertEqual(rt.telemetry.read("error_rate").value, before)


class LockScopeTests(unittest.TestCase):
    """The state lock serialises state transitions. It must not be held
    across anything slow, because everything else that touches state waits
    behind it -- a screenshot submission, a status request, the next turn."""

    class _SlowSink:
        name = "slow"

        def __init__(self, seconds: float = 0.6) -> None:
            self.seconds = seconds
            self.speaking = threading.Event()
            self.lines: list[str] = []

        def speak(self, text: str) -> None:
            self.speaking.set()
            time.sleep(self.seconds)
            self.lines.append(text)

    def test_the_state_lock_is_free_while_an_intervention_is_being_delivered(self) -> None:
        # ``sink.speak`` is an HTTP call to Agora with an eight-second
        # timeout. Under the lock, one stalled request freezes the pipeline
        # for the whole timeout in the middle of a live incident.
        clock = ManualClock(start=T0)
        sink = self._SlowSink()
        rt = runtime(clock, sink=sink)
        self.addCleanup(rt.close)

        def turn(text: str, turn_id: str) -> None:
            clock.advance(5)
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001", turn_id=turn_id, role="human", text=text,
                    final=True, timestamp=clock.now(),
                    source_modality=SourceModality.VOICE,
                )
            )

        speaker = threading.Thread(target=turn, args=("Let's rollback Core.", "slow-1"))
        speaker.start()
        self.addCleanup(speaker.join)

        self.assertTrue(sink.speaking.wait(timeout=5), "the sink was never reached")
        acquired = rt.pipeline._lock.acquire(timeout=0.25)
        if acquired:
            rt.pipeline._lock.release()
        self.assertTrue(acquired, "the state lock was held across the speak call")

    def test_the_intervention_is_still_delivered_and_recorded(self) -> None:
        # Deferring delivery must not lose it.
        clock = ManualClock(start=T0)
        sink = self._SlowSink(seconds=0.0)
        rt = runtime(clock, sink=sink)
        self.addCleanup(rt.close)
        clock.advance(5)
        result = rt.pipeline.handle_transcript(
            TranscriptEvent(
                uid="1001", turn_id="slow-2", role="human", text="Let's rollback Core.",
                final=True, timestamp=clock.now(), source_modality=SourceModality.VOICE,
            )
        )
        self.assertTrue(result.spoke)
        self.assertEqual(len(sink.lines), 1)
        self.assertTrue(rt.store.interventions())

    def test_one_failed_delivery_does_not_abandon_the_others(self) -> None:
        class FlakySink:
            name = "flaky"

            def __init__(self) -> None:
                self.calls = 0
                self.delivered: list[str] = []

            def speak(self, text: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise InterventionError("simulated first-delivery failure", sink=self.name)
                self.delivered.append(text)

        sink = FlakySink()
        clock = ManualClock(start=T0)
        rt = runtime(clock, sink=sink)
        self.addCleanup(rt.close)
        for index, text in enumerate(
            ("Let's rollback Core.", "Pool utilization looks fine, like 40%.")
        ):
            clock.advance(50)
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001", turn_id=f"flaky-{index}", role="human", text=text,
                    final=True, timestamp=clock.now(),
                    source_modality=SourceModality.VOICE,
                )
            )
        self.assertEqual(sink.calls, 2, "the second intervention was never attempted")
        self.assertEqual(len(sink.delivered), 1)


class ResilienceTests(PipelineTestCase):
    def test_duplicate_turn_ids_do_not_double_count(self) -> None:
        # This used to assert only "at least one fact, and no crash", because
        # deduplication lived in the HTTP handler and the pipeline itself
        # genuinely did double-count. It now claims the turn id at the one
        # entry point every transport shares, so the guarantee is exact.
        event = TranscriptEvent(uid="1001", turn_id="dup", role="human",
                                text="Payments are throwing 500s.", final=True,
                                timestamp=self.clock.now())
        first = self.rt.pipeline.handle_transcript(event)
        second = self.rt.pipeline.handle_transcript(event)

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.claims, ())
        facts = self.rt.store.snapshot(captured_at=self.clock.now()).facts
        self.assertEqual(len(facts), 1, "a replayed utterance became two facts")

    def test_an_interim_event_does_not_consume_its_turn_id(self) -> None:
        # Interim transcripts share the final one's turn id. Claiming on the
        # interim would make the final -- the only one that carries meaning --
        # look like a duplicate and be dropped entirely.
        interim = TranscriptEvent(uid="1001", turn_id="partial", role="human",
                                  text="Payments are throw", final=False,
                                  timestamp=self.clock.now())
        self.rt.pipeline.handle_transcript(interim)
        self.clock.advance(1)
        final = TranscriptEvent(uid="1001", turn_id="partial", role="human",
                                text="Payments are throwing 500s.", final=True,
                                timestamp=self.clock.now())
        result = self.rt.pipeline.handle_transcript(final)
        self.assertFalse(result.duplicate)
        self.assertTrue(result.claims)

    def test_out_of_order_events_are_ordered_by_timestamp_on_read(self) -> None:
        late = TranscriptEvent(uid="1001", turn_id="t-late", role="human",
                               text="Payments are throwing 500s.", final=True,
                               timestamp=T0 + timedelta(seconds=100))
        early = TranscriptEvent(uid="1001", turn_id="t-early", role="human",
                                text="We are seeing timeouts on checkout.", final=True,
                                timestamp=T0 + timedelta(seconds=10))
        self.rt.pipeline.handle_transcript(late)
        self.rt.pipeline.handle_transcript(early)
        timeline = self.rt.store.timeline()
        self.assertEqual([e.occurred_at for e in timeline],
                         sorted(e.occurred_at for e in timeline))

    def test_an_extraction_failure_does_not_stop_the_loop(self) -> None:
        class Exploding:
            name = "exploding"

            def supports_vision(self) -> bool:
                return False

            def complete(self, request):  # noqa: ANN001
                raise RuntimeError("provider is down")

        self.rt.extraction._provider = Exploding()  # noqa: SLF001 - deliberate fault injection
        result = self.say("Let's rollback Core.")
        self.assertTrue(result.degraded)
        self.assertEqual(result.errors, ())

        # And the pipeline still works once the provider recovers.
        from backend.extraction.providers.deterministic import DeterministicProvider

        self.rt.extraction._provider = DeterministicProvider()  # noqa: SLF001
        recovered = self.say("Payments are throwing 500s.", advance=5.0)
        self.assertFalse(recovered.degraded)

    def test_events_are_published_for_the_ui(self) -> None:
        with self.rt.events.subscribe() as subscription:
            self.say("Pool utilization looks fine, like 40%.", uid="1002")
            kinds = {event.kind for event in subscription.drain()}
        self.assertIn("transcript", kinds)
        self.assertIn("claim", kinds)
        self.assertIn("intervention", kinds)
        self.assertIn("evidence", kinds)


if __name__ == "__main__":
    unittest.main()
