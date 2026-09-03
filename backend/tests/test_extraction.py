"""Extraction service tests.

The properties under test are the ones that make extraction safe to build on:
provenance can never come from the model, invalid output is rejected without
taking the pipeline down, provider failure degrades rather than raises, and
the vocabulary the model may reference is constrained to things the rest of
the system can actually resolve.
"""

from __future__ import annotations

import json
import unittest

from backend.common.enums import ActionKind, ClaimType, DecisionStance, SourceModality
from backend.common.errors import ProviderError
from backend.extraction.contracts import ProviderRequest, ProviderResponse
from backend.extraction.providers.deterministic import DeterministicProvider
from backend.extraction.service import ExtractionService
from backend.risk_engine.topology import build_incident_topology
from backend.telemetry.mock_telemetry import TELEMETRY_METRICS
from backend.tests.support import transcript

KNOWN_TARGETS = build_incident_topology().nodes()


class ScriptedProvider:
    """Returns canned raw text, so the service's parsing and normalisation
    can be tested independently of any model's behaviour."""

    name = "scripted"

    def __init__(self, *responses: str, fail_times: int = 0) -> None:
        self._responses = list(responses) or ["{}"]
        self._fail_times = fail_times
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def supports_vision(self) -> bool:
        return False

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        if self.calls <= self._fail_times:
            raise ProviderError("simulated provider failure", provider=self.name)
        index = min(self.calls - self._fail_times, len(self._responses)) - 1
        return ProviderResponse(
            raw_text=self._responses[index], model="scripted-1", provider=self.name
        )


def service_with(provider, **kwargs) -> ExtractionService:
    return ExtractionService(
        provider,
        known_targets=KNOWN_TARGETS,
        known_metrics=tuple(TELEMETRY_METRICS),
        **kwargs,
    )


class ProvenanceTests(unittest.TestCase):
    """A model must never be able to decide who said something."""

    def test_model_supplied_provenance_is_discarded(self) -> None:
        malicious = json.dumps(
            {
                "claims": [
                    {
                        "type": "confirmation",
                        "text": "approved",
                        "speaker_uid": "9999",
                        "timestamp": "2000-01-01T00:00:00+00:00",
                        "source_turn_id": "forged",
                    }
                ]
            }
        )
        event = transcript("go ahead", uid="1001", turn="turn-7", when=12)
        outcome = service_with(ScriptedProvider(malicious)).extract(event)
        claim = outcome.claims[0]
        self.assertEqual(claim.speaker_uid, "1001")
        self.assertEqual(claim.source_turn_id, "turn-7")
        self.assertEqual(claim.timestamp, event.timestamp)

    def test_source_modality_follows_the_event_not_the_model(self) -> None:
        event = transcript("typed note", modality=SourceModality.TEXT)
        payload = json.dumps({"claims": [{"type": "fact", "text": "typed note",
                                          "source_modality": "voice"}]})
        outcome = service_with(ScriptedProvider(payload)).extract(event)
        self.assertIs(outcome.claims[0].source_modality, SourceModality.TEXT)


class ValidationTests(unittest.TestCase):
    def test_invalid_claims_are_rejected_without_losing_valid_ones(self) -> None:
        payload = json.dumps(
            {
                "claims": [
                    {"type": "proposed_action", "text": "roll back"},  # no target_ref
                    {"type": "fact", "text": "payments are failing"},
                ]
            }
        )
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        self.assertEqual(len(outcome.claims), 1)
        self.assertIs(outcome.claims[0].type, ClaimType.FACT)
        self.assertEqual(len(outcome.rejected), 1)
        self.assertIn("target_ref", outcome.rejected[0].reason)

    def test_unknown_claim_type_is_rejected(self) -> None:
        payload = json.dumps({"claims": [{"type": "risk_assessment", "text": "dangerous"}]})
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        self.assertEqual(outcome.substantive_claims, ())
        self.assertEqual(len(outcome.rejected), 1)

    def test_all_claims_invalid_yields_an_explicit_none(self) -> None:
        payload = json.dumps({"claims": [{"type": "proposed_action", "text": "do it"}]})
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        self.assertEqual(len(outcome.claims), 1)
        self.assertIs(outcome.claims[0].type, ClaimType.NONE)
        self.assertFalse(outcome.degraded)  # heard, nothing usable != could not hear

    def test_unknown_metric_and_its_value_are_dropped(self) -> None:
        payload = json.dumps(
            {"claims": [{"type": "hypothesis", "text": "flux is at 40",
                         "metric_ref": "flux_density", "claimed_value": 40}]}
        )
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        claim = outcome.claims[0]
        self.assertIsNone(claim.metric_ref)
        self.assertIsNone(claim.claimed_value)

    def test_known_target_and_metric_survive(self) -> None:
        payload = json.dumps(
            {"claims": [{"type": "hypothesis", "text": "pool is at 40%",
                         "target_ref": "core-db", "metric_ref": "pool_utilization",
                         "claimed_value": 40, "claimed_unit": "%"}]}
        )
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        claim = outcome.claims[0]
        self.assertEqual(claim.target_ref, "core-db")
        self.assertEqual(claim.metric_ref, "pool_utilization")
        self.assertEqual(claim.claimed_value, 40)


class ResponseToleranceTests(unittest.TestCase):
    def test_fenced_json_is_parsed(self) -> None:
        payload = '```json\n{"claims":[{"type":"fact","text":"payments are failing"}]}\n```'
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        self.assertIs(outcome.claims[0].type, ClaimType.FACT)

    def test_prose_wrapped_json_is_recovered(self) -> None:
        payload = 'Sure! Here you go: {"claims":[{"type":"fact","text":"payments are failing"}]} hope that helps'
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        self.assertIs(outcome.claims[0].type, ClaimType.FACT)

    def test_unparseable_response_degrades_rather_than_raising(self) -> None:
        outcome = service_with(ScriptedProvider("not json at all")).extract(transcript("x"))
        self.assertTrue(outcome.degraded)
        self.assertIs(outcome.claims[0].type, ClaimType.NONE)

    def test_missing_claims_array_degrades(self) -> None:
        outcome = service_with(ScriptedProvider('{"result": "ok"}')).extract(transcript("x"))
        self.assertTrue(outcome.degraded)

    def test_empty_response_degrades(self) -> None:
        outcome = service_with(ScriptedProvider("")).extract(transcript("x"))
        self.assertTrue(outcome.degraded)


class FailureHandlingTests(unittest.TestCase):
    def test_transient_failure_is_retried_within_the_bound(self) -> None:
        provider = ScriptedProvider('{"claims":[{"type":"fact","text":"payments are failing"}]}',
                                    fail_times=1)
        outcome = service_with(provider, max_attempts=2).extract(transcript("x"))
        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.attempts, 2)

    def test_retries_are_bounded_and_failure_is_survivable(self) -> None:
        provider = ScriptedProvider("{}", fail_times=99)
        outcome = service_with(provider, max_attempts=2).extract(transcript("x"))
        self.assertTrue(outcome.degraded)
        self.assertEqual(provider.calls, 2)
        self.assertIs(outcome.claims[0].type, ClaimType.NONE)
        self.assertIsNotNone(outcome.failure_reason)

    def test_an_unexpected_provider_exception_does_not_escape(self) -> None:
        class Exploding:
            name = "exploding"

            def supports_vision(self) -> bool:
                return False

            def complete(self, request):  # noqa: ANN001
                raise RuntimeError("kaboom")

        outcome = service_with(Exploding(), max_attempts=1).extract(transcript("x"))
        self.assertTrue(outcome.degraded)
        self.assertIn("kaboom", outcome.failure_reason or "")


class LocalFallbackTests(unittest.TestCase):
    """A dead hosted model must not cost the utterance.

    Without a fallback the loop survives a provider outage but survives
    *blind*: the turn produces a ``none`` claim and whatever was said in it is
    gone. If that turn was the one proposing a rollback, AEGIS had nothing to
    reason about at the moment it mattered most. A complete offline extractor
    already ships in this repository, so going blind was a wiring gap rather
    than a constraint.
    """

    def _service(self, provider, **kwargs) -> ExtractionService:
        return service_with(provider, fallback_provider=DeterministicProvider(), **kwargs)

    def test_a_dead_provider_falls_back_to_local_extraction(self) -> None:
        provider = ScriptedProvider("{}", fail_times=99)
        outcome = self._service(provider, max_attempts=2).extract(
            transcript("Let's rollback core-db to the last version.")
        )
        self.assertFalse(outcome.degraded, "the turn was dropped despite a working fallback")
        self.assertIn("fallback", outcome.provider)
        self.assertIn(
            ClaimType.PROPOSED_ACTION,
            [claim.type for claim in outcome.claims],
            "the rollback proposal was lost while the provider was down",
        )
        # The outage is still on the record.
        self.assertIsNotNone(outcome.failure_reason)

    def test_malformed_output_also_falls_back(self) -> None:
        # The call succeeded and the content was garbage, which the retry loop
        # does not cover -- so this is the only thing between a hallucinating
        # model and a lost utterance.
        provider = ScriptedProvider("not json at all")
        outcome = self._service(provider, max_attempts=1).extract(
            transcript("Let's rollback core-db to the last version.")
        )
        self.assertFalse(outcome.degraded)
        self.assertIn("fallback", outcome.provider)

    def test_without_a_fallback_the_behaviour_is_unchanged(self) -> None:
        provider = ScriptedProvider("{}", fail_times=99)
        outcome = service_with(provider, max_attempts=2).extract(transcript("x"))
        self.assertTrue(outcome.degraded)
        self.assertIs(outcome.claims[0].type, ClaimType.NONE)

    def test_a_fallback_that_finds_nothing_reports_the_outage_honestly(self) -> None:
        # Filler carries no claim. Reporting that as a successful extraction
        # would hide a provider outage behind an empty result, so the outcome
        # has to stay degraded when the fallback recovered nothing.
        provider = ScriptedProvider("{}", fail_times=99)
        outcome = self._service(provider, max_attempts=1).extract(transcript("so anyway"))
        self.assertTrue(outcome.degraded)

    def test_the_fallback_is_never_cached(self) -> None:
        # The cache holds what the *configured* provider said. Seeding it from
        # the fallback would serve degraded extractions after the outage ends.
        provider = ScriptedProvider("{}", fail_times=99)
        service = self._service(provider, max_attempts=1)
        text = "Let's rollback core-db to the last version."
        service.extract(transcript(text, turn="turn-a"))
        self.assertEqual(service.cache_size, 0)


class ContextTests(unittest.TestCase):
    def test_recent_turns_accumulate_and_are_bounded(self) -> None:
        provider = ScriptedProvider('{"claims":[{"type":"none","text":""}]}')
        service = service_with(provider, context_turns=3)
        for index in range(5):
            service.extract(transcript(f"utterance {index}", turn=f"t{index}"))
        request_context = service._recent  # noqa: SLF001 - asserting the bound directly
        self.assertEqual(len(request_context), 3)
        self.assertIn("utterance 4", request_context[-1])

    def test_context_is_remembered_even_when_extraction_fails(self) -> None:
        service = service_with(ScriptedProvider("{}", fail_times=99), max_attempts=1)
        service.extract(transcript("something important"))
        self.assertEqual(len(service._recent), 1)  # noqa: SLF001


class DeterministicProviderTests(unittest.TestCase):
    """The offline provider has to carry the golden demo unaided."""

    def setUp(self) -> None:
        self.service = service_with(DeterministicProvider())

    def extract_one(self, text: str, **kwargs):
        outcome = self.service.extract(transcript(text, **kwargs))
        return outcome.claims

    def test_observation_is_a_fact(self) -> None:
        claims = self.extract_one("Payments are throwing 500s, seeing timeouts")
        self.assertIs(claims[0].type, ClaimType.FACT)

    def test_hedged_metric_reading_is_a_hypothesis_with_the_figure(self) -> None:
        claims = self.extract_one("Pool utilization looks fine, like 40%")
        claim = claims[0]
        self.assertIs(claim.type, ClaimType.HYPOTHESIS)
        self.assertEqual(claim.metric_ref, "pool_utilization")
        self.assertEqual(claim.claimed_value, 40.0)
        self.assertEqual(claim.claimed_unit, "%")

    def test_a_bare_metric_reading_is_still_a_hypothesis(self) -> None:
        # The product exists because recited figures get treated as fact.
        claims = self.extract_one("pool utilization is 40%")
        self.assertIs(claims[0].type, ClaimType.HYPOTHESIS)

    def test_proposal_is_extracted_with_target_and_kind(self) -> None:
        claims = self.extract_one("Let's rollback Core to the last version")
        action = next(c for c in claims if c.type is ClaimType.PROPOSED_ACTION)
        self.assertEqual(action.target_ref, "core-db")
        self.assertIs(action.action_kind, ActionKind.ROLLBACK)

    def test_one_turn_can_yield_two_claims(self) -> None:
        claims = self.extract_one("Okay, fine, it is the pool then. Let's rollback Core.")
        types = [claim.type for claim in claims]
        self.assertIn(ClaimType.PROPOSED_ACTION, types)
        self.assertEqual(len(claims), 2)

    def test_explicit_version_is_captured(self) -> None:
        claims = self.extract_one("let's roll core-db back to v2.3")
        action = next(c for c in claims if c.type is ClaimType.PROPOSED_ACTION)
        self.assertEqual(action.target_schema_version, "v2.3")

    def test_hold_with_a_pending_action_is_a_resolution(self) -> None:
        claims = self.service.extract(
            transcript("Hold on, don't rollback yet"), pending_action_targets=("core-db",)
        ).claims
        self.assertIs(claims[0].type, ClaimType.HOLD)

    def test_confirmation_with_a_pending_action(self) -> None:
        claims = self.service.extract(
            transcript("Yes, go ahead"), pending_action_targets=("core-db",)
        ).claims
        self.assertIs(claims[0].type, ClaimType.CONFIRMATION)

    def test_hold_without_a_pending_action_is_a_decision(self) -> None:
        claims = self.extract_one("Let's hold off on the core-db rollback for now")
        decision = next(c for c in claims if c.type is ClaimType.DECISION)
        self.assertIs(decision.stance if hasattr(decision, "stance") else decision.decision_stance,
                      DecisionStance.HOLD)
        self.assertEqual(decision.target_ref, "core-db")

    def test_inflected_hold_is_recorded_with_the_right_polarity(self) -> None:
        """Regression: "holding off" is a hold.

        A substring list containing "hold off" misses it, because the word in
        the sentence is "holding" -- and the decision then lands in the
        ledger with the opposite stance, so the reversal check never fires on
        the failure mode it exists to catch.
        """
        for phrasing in (
            "We're holding off on the core-db rollback for now.",
            "We'll hold on the core-db rollback.",
            "We're not touching core-db.",
            "Let's hold off on core-db.",
        ):
            with self.subTest(phrasing=phrasing):
                claims = self.extract_one(phrasing)
                decision = next(c for c in claims if c.type is ClaimType.DECISION)
                self.assertIs(decision.decision_stance, DecisionStance.HOLD, phrasing)

    def test_a_go_ahead_decision_keeps_the_proceed_stance(self) -> None:
        claims = self.extract_one("Agreed, we're going with the core-db rollback.")
        decision = next(c for c in claims if c.type is ClaimType.DECISION)
        self.assertIs(decision.decision_stance, DecisionStance.PROCEED)

    def test_a_split_verb_particle_is_still_a_rollback(self) -> None:
        """Regression: people say "roll core-db back", not "roll back core-db".

        Missing the kind is not cosmetic -- the schema-compatibility check
        only runs for actions that change a schema surface, so an
        unrecognised rollback is a blast radius nobody checks.
        """
        for phrasing in (
            "Let's roll core-db back anyway.",
            "Let's roll back core-db.",
            "Let's rollback core-db.",
            "We should revert core-db.",
        ):
            with self.subTest(phrasing=phrasing):
                claims = self.extract_one(phrasing)
                action = next(c for c in claims if c.type is ClaimType.PROPOSED_ACTION)
                self.assertIs(action.action_kind, ActionKind.ROLLBACK, phrasing)

    def test_benign_no_phrases_are_not_read_as_refusals(self) -> None:
        claims = self.service.extract(
            transcript("no problem, that works for me"), pending_action_targets=("core-db",)
        ).claims
        self.assertNotIn(ClaimType.OVERRIDE, [claim.type for claim in claims])

    def test_filler_is_classified_as_none(self) -> None:
        self.assertIs(self.extract_one("okay")[0].type, ClaimType.NONE)

    def test_action_without_a_resolvable_target_does_not_invent_one(self) -> None:
        claims = self.extract_one("let's restart the thing")
        self.assertNotIn(ClaimType.PROPOSED_ACTION, [c.type for c in claims])

    def test_output_passes_through_the_same_validation_path_as_a_real_provider(self) -> None:
        outcome = self.service.extract(transcript("Let's rollback Core"))
        self.assertEqual(outcome.provider, "deterministic")
        self.assertEqual(outcome.rejected, ())


class FastPathTests(unittest.TestCase):
    """Acknowledgements are most of what people say and none of what matters.

    Sending them to a model costs a round trip on the critical path for a
    guaranteed empty answer, so they are answered locally -- but only when
    they are *unambiguously* content-free.
    """

    def setUp(self) -> None:
        self.provider = ScriptedProvider('{"claims": []}')
        self.service = service_with(self.provider)

    def test_a_backchannel_never_reaches_the_provider(self) -> None:
        for filler in ("mm", "uh huh", "yeah", "okay", "right, sure", "Mhm."):
            with self.subTest(filler=filler):
                outcome = self.service.extract(transcript(filler))
                self.assertEqual(outcome.provider, "fast_path")
                self.assertIs(outcome.claims[0].type, ClaimType.NONE)
        self.assertEqual(self.provider.calls, 0, "the model was consulted about filler")

    def test_the_fast_path_still_produces_a_well_formed_claim(self) -> None:
        # Downstream code reads provenance off every claim; a short-circuit
        # that returned a differently-shaped claim would be a latent crash.
        claim = self.service.extract(transcript("yeah", uid="2002", turn="t-9")).claims[0]
        self.assertEqual(claim.speaker_uid, "2002")
        self.assertEqual(claim.source_turn_id, "t-9")

    def test_agreement_carrying_content_is_not_short_circuited(self) -> None:
        # "yeah, go ahead" while something is pending is a confirmation. The
        # fast path must not swallow it -- that would be an authorisation bug.
        for utterance in ("yeah, go ahead with the rollback", "okay, roll back core-db"):
            with self.subTest(utterance=utterance):
                self.provider.calls = 0
                outcome = self.service.extract(
                    transcript(utterance), pending_action_targets=("core-db",)
                )
                self.assertNotEqual(outcome.provider, "fast_path")
                self.assertEqual(self.provider.calls, 1)

    def test_the_fast_path_still_feeds_conversational_context(self) -> None:
        # Filler carries no claim but it is still a turn; dropping it from the
        # rolling window would misalign the context the model sees next.
        self.service.extract(transcript("okay"))
        self.service.extract(transcript("Let's rollback core-db."))
        prompt = self.provider.last_request.user_prompt
        self.assertIn("okay", prompt)


class ExtractionCacheTests(unittest.TestCase):
    """Short utterances repeat constantly in a war room. Caching them is only
    safe if the cached thing is the model's *answer*, not the finished claim."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(
            json.dumps(
                {
                    "claims": [
                        {
                            "type": "hypothesis",
                            "text": "the pool is saturated",
                            "confidence": "medium",
                        }
                    ]
                }
            )
        )
        self.service = service_with(self.provider)

    def test_a_repeated_short_utterance_is_answered_from_cache(self) -> None:
        first = self.service.extract(transcript("pool is saturated", turn="t-1"))
        second = self.service.extract(transcript("pool is saturated", turn="t-2"))
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(first.provider, "scripted")
        self.assertEqual(second.provider, "cache")

    def test_a_cache_hit_re_derives_provenance_from_the_new_event(self) -> None:
        # The red line: a cached claim must never carry the speaker or turn of
        # the utterance that populated the entry, or the ledger attributes one
        # person's words to another.
        self.service.extract(transcript("pool is saturated", uid="1001", turn="t-1", when=0))
        second = self.service.extract(
            transcript("pool is saturated", uid="4004", turn="t-2", when=30)
        )
        claim = second.claims[0]
        self.assertEqual(claim.speaker_uid, "4004")
        self.assertEqual(claim.source_turn_id, "t-2")
        self.assertEqual(claim.text, "the pool is saturated")

    def test_the_same_words_are_not_shared_across_different_pending_context(self) -> None:
        # "go ahead" means something different when an action is awaiting a
        # decision. Keying on words alone would serve a confirmation into a
        # context where nothing was proposed.
        self.service.extract(transcript("go ahead"))
        self.service.extract(transcript("go ahead"), pending_action_targets=("core-db",))
        self.assertEqual(self.provider.calls, 2)

    def test_the_pending_set_is_matched_as_a_set_not_a_sequence(self) -> None:
        # Regression, found by the benchmark: keying on the ordered tuple made
        # the cache miss on every reordering of the pending list, so its hit
        # rate collapsed in exactly the long incidents it exists for.
        self.service.extract(
            transcript("pool is saturated", turn="t-1"),
            pending_action_targets=("core-db", "payment-api"),
        )
        second = self.service.extract(
            transcript("pool is saturated", turn="t-2"),
            pending_action_targets=("payment-api", "core-db"),
        )
        self.assertEqual(second.provider, "cache")
        self.assertEqual(self.provider.calls, 1)

    def test_a_genuinely_different_pending_set_still_misses(self) -> None:
        self.service.extract(
            transcript("go ahead", turn="t-1"), pending_action_targets=("core-db",)
        )
        self.service.extract(
            transcript("go ahead", turn="t-2"), pending_action_targets=("core-db", "payment-api")
        )
        self.assertEqual(self.provider.calls, 2)

    def test_long_utterances_are_not_cached(self) -> None:
        long_text = "the connection pool on core-db looks saturated to me right now honestly"
        self.service.extract(transcript(long_text, turn="t-1"))
        self.service.extract(transcript(long_text, turn="t-2"))
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.service.cache_size, 0)

    def test_the_cache_is_bounded_and_evicts_least_recently_used(self) -> None:
        service = service_with(ScriptedProvider('{"claims": []}'), cache_size=2)
        for index in range(3):
            service.extract(transcript(f"phrase {index}"))
        self.assertLessEqual(service.cache_size, 2)

    def test_the_cache_survives_concurrent_extraction(self) -> None:
        # Extraction runs outside the pipeline lock so a slow provider cannot
        # stall state transitions; that is exactly what makes this structure
        # reachable from more than one thread.
        import threading

        service = service_with(ScriptedProvider('{"claims": []}'), cache_size=8)
        errors: list[BaseException] = []

        def hammer(worker: int) -> None:
            try:
                for index in range(50):
                    service.extract(transcript(f"phrase {index % 12}", turn=f"w{worker}-{index}"))
            except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertLessEqual(service.cache_size, 8)

    def test_caching_can_be_disabled_entirely(self) -> None:
        provider = ScriptedProvider('{"claims": []}')
        service = service_with(provider, cache_size=0)
        service.extract(transcript("pool is saturated", turn="t-1"))
        service.extract(transcript("pool is saturated", turn="t-2"))
        self.assertEqual(provider.calls, 2)


if __name__ == "__main__":
    unittest.main()

class SemanticRulesTests(unittest.TestCase):
    def _service(self, provider: ScriptedProvider) -> ExtractionService:
        return service_with(provider, fallback_provider=None)

    def test_fact(self):
        outcome = self._service(ScriptedProvider('{"claims": [{"type": "fact", "text": "foo"}]}')).extract(transcript("foo"))
        self.assertEqual(outcome.claims[0].type, ClaimType.FACT)

    def test_hypothesis(self):
        outcome = self._service(ScriptedProvider('{"claims": [{"type": "hypothesis", "text": "foo"}]}')).extract(transcript("foo"))
        self.assertEqual(outcome.claims[0].type, ClaimType.HYPOTHESIS)
        
    def test_proposed_action(self):
        outcome = self._service(ScriptedProvider('{"claims": [{"type": "proposed_action", "text": "foo", "target_ref": "payment-api", "action_kind": "restart"}]}')).extract(transcript("foo"))
        self.assertEqual(outcome.claims[0].type, ClaimType.PROPOSED_ACTION)

    def test_novel_target_ref_is_preserved(self):
        outcome = self._service(ScriptedProvider('{"claims": [{"type": "proposed_action", "text": "foo", "target_ref": "unknown-service", "action_kind": "restart"}]}')).extract(transcript("foo"))
        self.assertEqual(outcome.claims[0].type, ClaimType.PROPOSED_ACTION)
        self.assertEqual(outcome.claims[0].target_ref, "unknown-service")

    def test_llm_failure_degraded_path_without_fallback(self):
        provider = ScriptedProvider("{}", fail_times=99)
        outcome = self._service(provider).extract(transcript("foo"))
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.claims[0].type, ClaimType.NONE)

