"""
HTTP surface tests.

Driven through Starlette's TestClient, so routing, validation, the auth
guard, the rate limiter and the error envelope are all exercised as a real
client would hit them.

The security tests matter more than they look: an unauthenticated endpoint
that accepts a ``confirmation`` claim is a path to an action being treated
as authorised that no human approved.
"""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from backend.api.app import AegisApi
from backend.common.clock import ManualClock
from backend.common.config import (
    ApiConfig,
    AppConfig,
    GovernorConfig,
    Secret,
    load_config,
)
from backend.pipeline.factory import build_runtime
from backend.pipeline.sinks import RecordingSink
from backend.tests.support import T0

TOKEN = "test-token-value"


def make_app(*, token: str = TOKEN, rpm: int = 600):
    base = load_config(env={}, dotenv_path=None)
    clock = ManualClock(start=T0)
    config = AppConfig(
        agora=base.agora,
        llm=base.llm,
        governor=GovernorConfig(rate_limit_seconds=45.0),
        pipeline=base.pipeline,
        api=ApiConfig(ingest_token=Secret(token), ingest_rate_limit_per_minute=rpm),
        database_path=base.database_path,
        incident_id="api-test",
        log_level="CRITICAL",
    )
    runtime = build_runtime(config, clock=clock, sink=RecordingSink(clock=clock),
                            database_path=":memory:")
    api = AegisApi(runtime, clock=clock)
    return api, runtime, clock


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.api, self.runtime, self.clock = make_app()
        self.app = self.api.build()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {TOKEN}"}

    def post(self, path: str, payload: dict, *, authed: bool = True):
        return self.client.post(path, json=payload, headers=self.auth() if authed else {})

    def wait_for_worker(self) -> None:
        """Block until the ingest worker has finished everything queued.

        Waiting on "the timeline is non-empty" only works for the first turn
        of a test; every later assertion would race the worker. The queue's
        own unfinished-task count is the real completion signal.
        """
        drained = self.api.worker.drain(timeout=5.0)
        self.assertTrue(drained, "the ingest worker did not drain in time")


class ReadRouteTests(ApiTestCase):
    def test_health_reports_the_runtime_it_actually_has(self) -> None:
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["incident_id"], "api-test")
        self.assertEqual(body["extraction_provider"], "deterministic")
        self.assertEqual(body["rate_limit_seconds"], 45.0)
        self.assertFalse(body["agora_authenticated"])

    def test_state_returns_an_empty_but_well_formed_incident(self) -> None:
        body = self.client.get("/api/state").json()
        for key in ("facts", "hypotheses", "decisions", "proposed_actions",
                    "evidence", "interventions", "timeline"):
            self.assertIn(key, body)
            self.assertEqual(body[key], [])

    def test_topology_is_inspectable(self) -> None:
        body = self.client.get("/api/topology").json()
        self.assertIn("core-db", body["nodes"])
        kinds = {edge["type"] for edge in body["edges"]}
        self.assertEqual(kinds, {"depends_on", "reads_schema", "compatible_with"})

    def test_telemetry_lists_exactly_the_four_fixed_metrics(self) -> None:
        metrics = self.client.get("/api/telemetry").json()["metrics"]
        self.assertEqual(
            {m["name"] for m in metrics},
            {"pool_utilization", "error_rate", "p99_latency", "schema_version"},
        )


class AuthTests(ApiTestCase):
    def test_ingest_requires_a_token(self) -> None:
        response = self.post("/api/transcript", {"uid": "1001", "text": "hi", "final": True},
                             authed=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_a_wrong_token_is_rejected(self) -> None:
        response = self.client.post(
            "/api/transcript",
            json={"uid": "1001", "text": "hi", "final": True},
            headers={"Authorization": "Bearer wrong-token-value"},
        )
        self.assertEqual(response.status_code, 401)

    def test_a_non_bearer_scheme_is_rejected(self) -> None:
        response = self.client.post(
            "/api/transcript",
            json={"uid": "1001", "text": "hi", "final": True},
            headers={"Authorization": f"Basic {TOKEN}"},
        )
        self.assertEqual(response.status_code, 401)

    def test_confirmations_cannot_be_injected_without_the_token(self) -> None:
        # The load-bearing case: an unauthenticated confirmation would be a
        # path to an unauthorised action.
        response = self.post("/api/text", {"text": "yes, go ahead"}, authed=False)
        self.assertEqual(response.status_code, 401)

    def test_read_routes_stay_open_for_the_dashboard(self) -> None:
        self.assertEqual(self.client.get("/api/state").status_code, 200)

    def test_auth_disabled_lets_ingest_through(self) -> None:
        api, runtime, _ = make_app(token="")
        with TestClient(api.build()) as client:
            response = client.post("/api/transcript",
                                   json={"uid": "1001", "text": "hi there team", "final": True})
            self.assertEqual(response.status_code, 202)


class ValidationTests(ApiTestCase):
    def test_missing_uid_is_a_400_not_a_500(self) -> None:
        response = self.post("/api/transcript", {"text": "hello", "final": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_empty_text_is_rejected_on_the_text_channel(self) -> None:
        self.assertEqual(self.post("/api/text", {"text": "   "}).status_code, 400)

    def test_evidence_requires_metric_and_value(self) -> None:
        self.assertEqual(self.post("/api/evidence", {"value": 91}).status_code, 400)
        self.assertEqual(self.post("/api/evidence", {"metric_name": "pool_utilization"}).status_code, 400)

    def test_evidence_certainty_is_constrained(self) -> None:
        response = self.post("/api/evidence", {"metric_name": "pool_utilization", "value": 91,
                                                "extraction_certainty": "0.87"})
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_is_a_400(self) -> None:
        response = self.client.post("/api/text", content=b"{not json", headers=self.auth())
        self.assertEqual(response.status_code, 400)

    def test_oversized_body_is_rejected(self) -> None:
        response = self.client.post(
            "/api/text",
            content=b'{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}',
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 413)


class IngestTests(ApiTestCase):
    def test_transcript_is_accepted_asynchronously(self) -> None:
        response = self.post("/api/transcript",
                             {"uid": "1001", "text": "Payments are throwing 500s.", "final": True})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.assertIn("turn_id", response.json())

    def test_an_accepted_transcript_reaches_the_state_store(self) -> None:
        self.post("/api/transcript",
                  {"uid": "1001", "text": "Payments are throwing 500s.", "final": True})
        self.wait_for_worker()
        self.assertGreaterEqual(len(self.client.get("/api/state").json()["facts"]), 1)

    def test_typed_text_is_marked_as_a_text_modality_claim(self) -> None:
        self.post("/api/text", {"uid": "1002", "text": "Payments are throwing 500s."})
        self.wait_for_worker()
        facts = self.client.get("/api/state").json()["facts"]
        self.assertEqual(facts[0]["source_modality"], "text")

    def test_evidence_submission_is_processed_synchronously(self) -> None:
        response = self.post("/api/evidence", {"metric_name": "pool_utilization", "value": 91,
                                                "unit": "%", "uploader_uid": "1002"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("evidence_id", response.json())
        self.assertGreaterEqual(len(self.client.get("/api/state").json()["evidence"]), 1)

    def test_setting_a_mock_metric_changes_what_telemetry_reports(self) -> None:
        self.post("/api/telemetry/set", {"metric_name": "pool_utilization", "value": 38})
        metrics = {m["name"]: m["current_value"] for m in self.client.get("/api/telemetry").json()["metrics"]}
        self.assertEqual(metrics["pool_utilization"], 38)

    def test_setting_an_unknown_metric_is_a_clean_error(self) -> None:
        response = self.post("/api/telemetry/set", {"metric_name": "nonsense", "value": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unknown_metric")


class RateLimitTests(unittest.TestCase):
    def test_ingest_is_rate_limited(self) -> None:
        api, runtime, _ = make_app(rpm=3)
        with TestClient(api.build()) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            payload = {"uid": "1001", "text": "hello team", "final": True}
            statuses = [
                client.post("/api/transcript", json=payload, headers=headers).status_code
                for _ in range(5)
            ]
        self.assertEqual(statuses[:3], [202, 202, 202])
        self.assertEqual(statuses[3], 429)
        self.assertEqual(statuses[4], 429)

    def test_rate_limit_response_says_when_to_retry(self) -> None:
        api, runtime, _ = make_app(rpm=1)
        with TestClient(api.build()) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            payload = {"uid": "1001", "text": "hello team", "final": True}
            client.post("/api/transcript", json=payload, headers=headers)
            blocked = client.post("/api/transcript", json=payload, headers=headers)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "rate_limited")


class ConditionalGetTests(ApiTestCase):
    """``/api/state`` is the hottest read in the system and it re-serialises
    the whole incident. The validator has to be right in both directions: a
    stale 304 hides an intervention, a useless 200 costs the projection."""

    def test_state_carries_a_validator(self) -> None:
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers.get("etag"))

    def test_an_unchanged_incident_is_a_304(self) -> None:
        first = self.client.get("/api/state")
        second = self.client.get(
            "/api/state", headers={"If-None-Match": first.headers["etag"]}
        )
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.headers["etag"], first.headers["etag"])
        self.assertEqual(second.content, b"")

    def test_a_write_invalidates_the_validator(self) -> None:
        first = self.client.get("/api/state")
        self.post("/api/transcript", {
            "uid": "1001", "turn_id": "etag-1", "text": "Pool utilization looks fine, like 40%.",
            "final": True,
        })
        self.wait_for_worker()

        conditional = self.client.get(
            "/api/state", headers={"If-None-Match": first.headers["etag"]}
        )
        self.assertEqual(conditional.status_code, 200, "the client would have missed a claim")
        self.assertNotEqual(conditional.headers["etag"], first.headers["etag"])

    def test_a_stale_validator_is_never_honoured(self) -> None:
        # A validator from a different incident, or an invented one, must not
        # short-circuit into a 304 -- that would serve one incident's cache
        # entry as another's state.
        response = self.client.get("/api/state", headers={"If-None-Match": 'W/"other-99"'})
        self.assertEqual(response.status_code, 200)

    def test_reads_do_not_invalidate(self) -> None:
        first = self.client.get("/api/state")
        for _ in range(3):
            self.client.get("/api/state")
        again = self.client.get("/api/state")
        self.assertEqual(again.headers["etag"], first.headers["etag"])


class IdempotentIngestTests(ApiTestCase):
    """Agora redelivers. A retried turn must not become a second claim in the
    ledger, and the client has to be able to tell the two cases apart."""

    def _turn(self, turn_id: str) -> dict:
        return {
            "uid": "1001",
            "turn_id": turn_id,
            "text": "Pool utilization looks fine, like 40%.",
            "final": True,
        }

    def test_a_replayed_turn_is_acknowledged_but_not_re_ingested(self) -> None:
        first = self.post("/api/transcript", self._turn("dup-1"))
        self.wait_for_worker()
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])

        second = self.post("/api/transcript", self._turn("dup-1"))
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])

    def test_the_ledger_records_the_turn_once(self) -> None:
        for _ in range(4):
            self.post("/api/transcript", self._turn("dup-2"))
            self.wait_for_worker()
        view = self.client.get("/api/state").json()
        from_turn = [
            (bucket, claim)
            for bucket in ("facts", "hypotheses", "proposed_actions", "decisions")
            for claim in view[bucket]
            if claim.get("source_turn_id") == "dup-2"
        ]
        self.assertTrue(from_turn, "the turn was never ingested at all")
        self.assertEqual(
            len({claim["claim_id"] for _bucket, claim in from_turn}),
            len(from_turn),
            "the same turn produced duplicate claim ids",
        )
        self.assertEqual(
            len(from_turn),
            len({(bucket, claim["text"]) for bucket, claim in from_turn}),
            "four deliveries of one turn became more than one claim each",
        )

    def test_a_different_turn_id_is_not_treated_as_a_replay(self) -> None:
        self.post("/api/transcript", self._turn("dup-3"))
        self.wait_for_worker()
        other = self.post("/api/transcript", self._turn("dup-4"))
        self.assertEqual(other.status_code, 202)
        self.assertFalse(other.json()["duplicate"])


class ResetRouteTests(ApiTestCase):
    """The demo affordance that matters most, and the one that must not be
    reachable without the token."""

    def _ingest(self, turn_id: str) -> None:
        self.post("/api/transcript", {
            "uid": "1001", "turn_id": turn_id,
            "text": "Pool utilization looks fine, like 40%.", "final": True,
        })
        self.wait_for_worker()

    def test_reset_requires_the_token(self) -> None:
        # It destroys the incident record. An unauthenticated caller must not
        # be able to wipe a demo mid-run.
        response = self.client.post("/api/reset")
        self.assertEqual(response.status_code, 401)

    def test_reset_empties_the_incident(self) -> None:
        self._ingest("reset-1")
        self.assertTrue(self.client.get("/api/state").json()["hypotheses"])

        response = self.post("/api/reset", {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reset"])

        state = self.client.get("/api/state").json()
        self.assertEqual(state["hypotheses"], [])
        self.assertEqual(state["facts"], [])

    def test_the_validator_changes_so_the_console_notices(self) -> None:
        self._ingest("reset-2")
        before = self.client.get("/api/state").headers["etag"]
        self.post("/api/reset", {})
        after = self.client.get("/api/state")
        self.assertEqual(after.status_code, 200)
        self.assertNotEqual(after.headers["etag"], before)

    def test_the_same_turn_can_be_replayed_after_a_reset(self) -> None:
        self._ingest("reset-3")
        self.post("/api/reset", {})
        response = self.post("/api/transcript", {
            "uid": "1001", "turn_id": "reset-3",
            "text": "Pool utilization looks fine, like 40%.", "final": True,
        })
        self.assertEqual(response.status_code, 202, "the rehearsed script could not be replayed")
        self.wait_for_worker()
        self.assertTrue(self.client.get("/api/state").json()["hypotheses"])

    def test_metrics_start_over(self) -> None:
        self._ingest("reset-4")
        self.assertTrue(self.client.get("/api/metrics").json()["counters"])
        self.post("/api/reset", {})
        self.assertEqual(self.client.get("/api/metrics").json()["counters"], {})


class MetricsRouteTests(ApiTestCase):
    """Every performance claim this backend makes should be checkable in one
    request rather than argued from the code."""

    def test_metrics_are_exposed_and_well_formed(self) -> None:
        payload = self.client.get("/api/metrics").json()
        for key in ("stages", "counters", "extraction", "scheduling", "ingest"):
            self.assertIn(key, payload)

    def test_stage_timings_appear_once_work_has_happened(self) -> None:
        self.post("/api/transcript", {
            "uid": "1001", "turn_id": "m-1",
            "text": "Pool utilization looks fine, like 40%.", "final": True,
        })
        self.wait_for_worker()
        stages = self.client.get("/api/metrics").json()["stages"]
        self.assertTrue(stages, "a processed turn recorded no timings")
        sample = next(iter(stages.values()))
        for key in ("count", "p50_ms", "p95_ms", "max_ms"):
            self.assertIn(key, sample)
        self.assertGreaterEqual(sample["p95_ms"], sample["p50_ms"])

    def test_the_fast_path_is_visible_in_the_counters(self) -> None:
        self.post("/api/transcript", {
            "uid": "1001", "turn_id": "m-2", "text": "mm hmm", "final": True,
        })
        self.wait_for_worker()
        counters = self.client.get("/api/metrics").json()["counters"]
        self.assertGreaterEqual(counters.get("extraction_fast_path_hits", 0), 1)


class EndToEndOverHttpTests(ApiTestCase):
    def test_the_first_killer_moment_works_over_the_api(self) -> None:
        self.post("/api/transcript",
                  {"uid": "1002", "text": "Pool utilization looks fine, like 40%.", "final": True})
        import time

        for _ in range(300):
            if self.runtime.sink.lines:
                break
            time.sleep(0.01)

        self.assertTrue(self.runtime.sink.lines, "no intervention was produced")
        spoken = self.runtime.sink.lines[0]
        self.assertIn("91", spoken)
        state = self.client.get("/api/state").json()
        self.assertEqual(len(state["interventions"]), 1)
        self.assertEqual(state["interventions"][0]["risk_tier"], "HIGH")


if __name__ == "__main__":
    unittest.main()
