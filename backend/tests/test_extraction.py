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

    def supports_vision(self) -> bool:
        return False

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
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

    def test_unknown_target_is_dropped_and_action_degrades_to_hypothesis(self) -> None:
        payload = json.dumps(
            {"claims": [{"type": "proposed_action", "text": "restart the flux capacitor",
                         "target_ref": "flux-capacitor", "action_kind": "restart"}]}
        )
        outcome = service_with(ScriptedProvider(payload)).extract(transcript("x"))
        claim = outcome.claims[0]
        self.assertIs(claim.type, ClaimType.HYPOTHESIS)
        self.assertIsNone(claim.target_ref)

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

    def test_filler_is_classified_as_none(self) -> None:
        self.assertIs(self.extract_one("okay")[0].type, ClaimType.NONE)

    def test_action_without_a_resolvable_target_does_not_invent_one(self) -> None:
        claims = self.extract_one("let's restart the thing")
        self.assertNotIn(ClaimType.PROPOSED_ACTION, [c.type for c in claims])

    def test_output_passes_through_the_same_validation_path_as_a_real_provider(self) -> None:
        outcome = self.service.extract(transcript("Let's rollback Core"))
        self.assertEqual(outcome.provider, "deterministic")
        self.assertEqual(outcome.rejected, ())


if __name__ == "__main__":
    unittest.main()
