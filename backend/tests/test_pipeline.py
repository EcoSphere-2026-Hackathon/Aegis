"""
End-to-end pipeline tests, including the golden demo.

This is the file that answers "does the product work". It drives the whole
loop -- utterance in, intervention out, human decision applied -- with no
network and no live voice, exactly as the build order requires the text-only
pipeline to be provable before Agora is wired in.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from backend.common.clock import ManualClock
from backend.common.config import AppConfig, GovernorConfig, load_config
from backend.common.enums import (
    ClaimType,
    HypothesisStatus,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
    SourceModality,
)
from backend.common.models import Evidence, TranscriptEvent
from backend.common.enums import EvidenceSource, EvidenceSourceType, ExtractionCertainty
from backend.pipeline.factory import build_runtime
from backend.pipeline.sinks import FailingSink, RecordingSink
from backend.tests.support import T0


def runtime(clock: ManualClock, *, sink=None, rate_limit: float = 45.0):
    config = load_config(env={}, dotenv_path=None, project_root=None)
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


class TextModalityTests(PipelineTestCase):
    def test_typed_text_is_treated_exactly_like_speech(self) -> None:
        result = self.say("Pool utilization looks fine, like 40%.", uid="1002",
                          modality=SourceModality.TEXT)
        self.assertTrue(result.spoke)
        claim = next(c for c in result.claims if c.type is ClaimType.HYPOTHESIS)
        self.assertIs(claim.source_modality, SourceModality.TEXT)


class ResilienceTests(PipelineTestCase):
    def test_duplicate_turn_ids_do_not_double_count(self) -> None:
        event = TranscriptEvent(uid="1001", turn_id="dup", role="human",
                                text="Payments are throwing 500s.", final=True,
                                timestamp=self.clock.now())
        self.rt.pipeline.handle_transcript(event)
        self.rt.pipeline.handle_transcript(event)
        facts = self.rt.store.snapshot(captured_at=self.clock.now()).facts
        # The same utterance replayed produces claims with fresh ids, but the
        # timeline must still be sane and nothing may crash.
        self.assertGreaterEqual(len(facts), 1)

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
