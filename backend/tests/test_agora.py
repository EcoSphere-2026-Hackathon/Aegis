"""
Agora integration tests, against a mock transport.

The live voice path is the one part of AEGIS that cannot be proven without
credentials and a real channel, and the honest position on that has not
changed: whether ``priority: INTERRUPT`` cuts through a human mid-sentence,
and whether manual turn detection keeps the agent quiet, are answerable only
by a live spike.

But "cannot be fully verified" had been allowed to mean "not tested at all",
and most of this integration is not about Agora's behaviour -- it is about
*ours*. Every one of the following is decidable here, with no account:

* the auth pair is the REST one, not the RTC one, and is a correct Basic header;
* the join payload carries the three properties the design depends on;
* the byte cap is enforced before the request, not after a rejection;
* a 401 is reported as a credential problem, not as a generic failure;
* no response body reaches an error context -- a body can echo the request,
  and the request carries an Authorization header;
* a failed speak becomes an ``InterventionError``, which is what returns the
  rate-limit window instead of silently losing an intervention.

What remains genuinely unverified is listed in the module docstring of
``scripts/check_agora.py`` and in the README, as a checklist a human runs
against a real channel.
"""

from __future__ import annotations

import base64
import json
import unittest

import httpx

from backend.agora.client import (
    SPEAK_MAX_BYTES,
    AgoraClient,
    AgoraSpeechSink,
    SpeakPriority,
)
from backend.common.config import AgoraConfig, Secret
from backend.common.errors import (
    AgoraAuthError,
    AgoraError,
    ConfigError,
    InterventionError,
)

APP_ID = "app-1234"


def config(**overrides) -> AgoraConfig:
    base = {
        "app_id": APP_ID,
        "channel_name": "incident-bridge",
        "customer_id": Secret("cust-id"),
        "customer_secret": Secret("cust-secret"),
        "base_url": "https://api.agora.io",
    }
    base.update(overrides)
    return AgoraConfig(**base)


class Recorder:
    """Captures the requests the client makes, and replies as told."""

    def __init__(self, *responses) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses) or [httpx.Response(200, json={"agent_id": "agent-1"})]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self._responses)) - 1
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content.decode("utf-8"))


def client_with(recorder: Recorder, cfg: AgoraConfig = None) -> AgoraClient:
    cfg = cfg or config()
    transport = httpx.MockTransport(recorder.handler)
    return AgoraClient(
        cfg,
        client=httpx.Client(base_url=cfg.base_url, transport=transport),
    )


class AuthenticationTests(unittest.TestCase):
    """The most common wiring mistake in this integration is using the App
    Certificate for REST auth. It fails as a 401 that looks like a typo."""

    def test_the_basic_header_is_built_from_the_customer_pair(self) -> None:
        recorder = Recorder()
        with client_with(recorder) as client:
            client.join()
        header = recorder.last.headers["authorization"]
        self.assertTrue(header.startswith("Basic "))
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        self.assertEqual(decoded, "cust-id:cust-secret")

    def test_missing_credentials_fail_before_any_request(self) -> None:
        recorder = Recorder()
        cfg = config(customer_id=Secret(""), customer_secret=Secret(""))
        with client_with(recorder, cfg) as client:
            with self.assertRaises(ConfigError) as caught:
                client.join()
        self.assertEqual(recorder.requests, [], "an unauthenticated request was sent")
        # The message has to name the right pair, or the operator spends the
        # demo window looking at the wrong credentials.
        self.assertIn("AGORA_CUSTOMER_ID", str(caught.exception.context["variables"]))

    def test_a_rejected_credential_is_reported_as_a_credential_problem(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                recorder = Recorder(httpx.Response(status, json={"detail": "nope"}))
                with client_with(recorder) as client:
                    with self.assertRaises(AgoraAuthError) as caught:
                        client.join()
                self.assertIn("Customer ID", str(caught.exception))
                self.assertEqual(caught.exception.context["status_code"], status)

    def test_no_response_body_reaches_an_error_context(self) -> None:
        # A body can echo the request, and the request carries the
        # Authorization header. Only the status code is safe to keep.
        recorder = Recorder(httpx.Response(500, json={"echo": {"authorization": "Basic secret"}}))
        with client_with(recorder) as client:
            with self.assertRaises(AgoraError) as caught:
                client.join()
        rendered = f"{caught.exception} {caught.exception.context}"
        self.assertNotIn("secret", rendered)
        self.assertNotIn("Basic", rendered)


class JoinPayloadTests(unittest.TestCase):
    """Three properties in this payload are load-bearing. Each has a specific
    failure mode if it is wrong, and none of them is visible in a demo until
    the demo is happening."""

    def setUp(self) -> None:
        self.recorder = Recorder()
        self.client = client_with(self.recorder)
        self.addCleanup(self.client.close)

    def test_the_agent_subscribes_to_every_participant(self) -> None:
        # Without the wildcard the agent hears one person, and a two-person
        # incident bridge silently becomes a one-person one.
        self.client.join()
        properties = self.recorder.body()["properties"]
        self.assertEqual(properties["remote_rtc_uids"], ["*"])

    def test_transcripts_are_requested_over_rtm(self) -> None:
        # The alternative data-stream mode is documented as not scaling past
        # a single user.
        self.client.join()
        properties = self.recorder.body()["properties"]
        self.assertTrue(properties["advanced_features"]["enable_rtm"])
        self.assertEqual(properties["parameters"]["data_channel"], "rtm")

    def test_silence_by_default_is_requested(self) -> None:
        # The single most important unverified assumption in the integration:
        # the agent must not answer merely because someone stopped talking.
        self.client.join()
        detection = self.recorder.body()["properties"]["turn_detection"]
        self.assertEqual(detection["config"]["start_of_speech"]["mode"], "manual")

    def test_silence_by_default_can_be_switched_off_for_a_spike(self) -> None:
        # If the spike shows manual mode does not suppress unsolicited turns,
        # the fallback has to be reachable by configuration, not a rewrite.
        self.client.join(silent_by_default=False)
        self.assertNotIn("turn_detection", self.recorder.body()["properties"])

    def test_the_channel_and_agent_uid_come_from_configuration(self) -> None:
        self.client.join()
        properties = self.recorder.body()["properties"]
        self.assertEqual(properties["channel"], "incident-bridge")
        self.assertEqual(properties["agent_rtc_uid"], "9000")

    def test_an_explicit_channel_overrides_the_configured_one(self) -> None:
        self.client.join(channel="other-bridge", agent_uid="9999")
        properties = self.recorder.body()["properties"]
        self.assertEqual(properties["channel"], "other-bridge")
        self.assertEqual(properties["agent_rtc_uid"], "9999")

    def test_the_project_id_is_in_the_path(self) -> None:
        self.client.join()
        self.assertIn(f"/projects/{APP_ID}/join", str(self.recorder.last.url))

    def test_a_join_without_an_agent_id_is_an_error_not_a_silent_success(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"status": "ok"}))
        with client_with(recorder) as client, self.assertRaises(AgoraError):
            client.join()

    def test_an_agent_id_in_camel_case_is_still_accepted(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"agentId": "agent-7"}))
        with client_with(recorder) as client:
            self.assertEqual(client.join(), "agent-7")


class SpeakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = Recorder(httpx.Response(200, json={}))
        self.client = client_with(self.recorder)
        self.addCleanup(self.client.close)

    def test_an_intervention_interrupts_and_cannot_be_talked_over(self) -> None:
        # An intervention a human can talk over halfway through may deliver
        # "rolling back Core will break" and stop -- naming a risk without
        # naming its consequence.
        self.client.speak("agent-1", "Hold — this will break payment-api.")
        body = self.recorder.body()
        self.assertEqual(body["priority"], SpeakPriority.INTERRUPT)
        self.assertFalse(body["interruptable"])

    def test_the_byte_cap_is_enforced_before_the_request(self) -> None:
        # Failing here is loud and local. Letting the service reject it means
        # discovering the limit during a live incident.
        recorder = Recorder(httpx.Response(200, json={}))
        with client_with(recorder) as client:
            with self.assertRaises(InterventionError):
                client.speak("agent-1", "x" * (SPEAK_MAX_BYTES + 1))
        self.assertEqual(recorder.requests, [], "an over-long speak was sent anyway")

    def test_the_cap_is_measured_in_bytes_not_characters(self) -> None:
        # A multi-byte character makes len(text) and the transport's limit
        # disagree, which is how a message passes locally and is rejected
        # over the wire.
        text = "—" * 200  # 600 bytes, 200 characters
        self.assertLessEqual(len(text), SPEAK_MAX_BYTES)
        recorder = Recorder(httpx.Response(200, json={}))
        with client_with(recorder) as client:
            with self.assertRaises(InterventionError):
                client.speak("agent-1", text)

    def test_exactly_the_limit_is_allowed(self) -> None:
        self.client.speak("agent-1", "x" * SPEAK_MAX_BYTES)
        self.assertEqual(len(self.recorder.requests), 1)

    def test_an_unsupported_priority_is_refused(self) -> None:
        # IGNORE appears in a local skill file and is not corroborated by the
        # official docs. Nothing load-bearing may depend on it.
        with self.assertRaises(InterventionError):
            self.client.speak("agent-1", "hello", priority="IGNORE")
        self.assertEqual(self.recorder.requests, [])

    def test_the_agent_id_is_in_the_path(self) -> None:
        self.client.speak("agent-42", "hello")
        self.assertIn("/agents/agent-42/speak", str(self.recorder.last.url))


class TransportFailureTests(unittest.TestCase):
    """Every failure mode a live demo can hit, and what it turns into."""

    def test_a_timeout_names_the_timeout(self) -> None:
        recorder = Recorder(httpx.TimeoutException("too slow"))
        with client_with(recorder) as client:
            with self.assertRaises(AgoraError) as caught:
                client.speak("agent-1", "hello")
        self.assertEqual(caught.exception.context["operation"], "speak")
        self.assertIn("timeout_seconds", caught.exception.context)

    def test_a_connection_failure_is_an_agora_error_not_a_crash(self) -> None:
        recorder = Recorder(httpx.ConnectError("no route"))
        with client_with(recorder) as client, self.assertRaises(AgoraError):
            client.join()

    def test_a_server_error_carries_the_status_and_the_operation(self) -> None:
        recorder = Recorder(httpx.Response(503, text="unavailable"))
        with client_with(recorder) as client:
            with self.assertRaises(AgoraError) as caught:
                client.leave("agent-1")
        self.assertEqual(caught.exception.context["status_code"], 503)
        self.assertEqual(caught.exception.context["operation"], "leave")

    def test_an_empty_body_is_not_a_parse_error(self) -> None:
        recorder = Recorder(httpx.Response(200, text=""))
        with client_with(recorder) as client:
            client.leave("agent-1")  # must not raise

    def test_a_non_json_body_is_not_a_parse_error(self) -> None:
        recorder = Recorder(httpx.Response(200, text="OK"))
        with client_with(recorder) as client:
            client.leave("agent-1")


class SpeechSinkTests(unittest.TestCase):
    """The sink is what the pipeline sees. Its contract is narrow: raise
    ``InterventionError`` on failure, so the governor gets its window back
    instead of the intervention being silently lost."""

    def test_a_delivered_intervention_reaches_the_speak_endpoint(self) -> None:
        recorder = Recorder(httpx.Response(200, json={}))
        with client_with(recorder) as client:
            AgoraSpeechSink(client, "agent-1").speak("Hold — check the pool first.")
        self.assertIn("/speak", str(recorder.last.url))

    def test_an_agora_failure_becomes_an_intervention_error(self) -> None:
        recorder = Recorder(httpx.Response(500))
        with client_with(recorder) as client:
            sink = AgoraSpeechSink(client, "agent-1")
            with self.assertRaises(InterventionError) as caught:
                sink.speak("Hold — check the pool first.")
        self.assertEqual(caught.exception.context["sink"], "agora")

    def test_an_auth_failure_also_becomes_an_intervention_error(self) -> None:
        # A credential problem discovered mid-incident must still return the
        # window rather than crashing the turn.
        recorder = Recorder(httpx.Response(401))
        with client_with(recorder) as client:
            with self.assertRaises(InterventionError):
                AgoraSpeechSink(client, "agent-1").speak("hello")

    def test_the_pipeline_returns_its_window_when_agora_fails(self) -> None:
        # The integration point that matters: a failed delivery must not cost
        # the rate-limit window, or one bad HTTP request silences AEGIS for
        # the next forty-five seconds of a live incident.
        from backend.common.clock import ManualClock
        from backend.common.enums import SourceModality
        from backend.common.models import TranscriptEvent
        from backend.tests.support import T0
        from backend.tests.test_pipeline import runtime

        recorder = Recorder(httpx.Response(500))
        clock = ManualClock(start=T0)
        with client_with(recorder) as client:
            rt = runtime(clock, sink=AgoraSpeechSink(client, "agent-1"))
            self.addCleanup(rt.close)
            clock.advance(5)
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001", turn_id="agora-1", role="human",
                    text="Let's rollback Core to the last version.", final=True,
                    timestamp=clock.now(), source_modality=SourceModality.VOICE,
                )
            )
            self.assertTrue(
                rt.governor.window_is_open(),
                "a failed delivery consumed the rate-limit window",
            )

    def test_a_successful_delivery_does_consume_the_window(self) -> None:
        # The control for the test above: without this, "the window is open"
        # would pass for a run in which nothing was ever spoken.
        from backend.common.clock import ManualClock
        from backend.common.enums import SourceModality
        from backend.common.models import TranscriptEvent
        from backend.tests.support import T0
        from backend.tests.test_pipeline import runtime

        recorder = Recorder(httpx.Response(200, json={}))
        clock = ManualClock(start=T0)
        with client_with(recorder) as client:
            rt = runtime(clock, sink=AgoraSpeechSink(client, "agent-1"))
            self.addCleanup(rt.close)
            clock.advance(5)
            rt.pipeline.handle_transcript(
                TranscriptEvent(
                    uid="1001", turn_id="agora-2", role="human",
                    text="Let's rollback Core to the last version.", final=True,
                    timestamp=clock.now(), source_modality=SourceModality.VOICE,
                )
            )
            self.assertIn("/speak", str(recorder.last.url))
            self.assertFalse(
                rt.governor.window_is_open(),
                "a delivered intervention did not consume the window",
            )


class ConfigurationSurfaceTests(unittest.TestCase):
    """Configuration problems must be obvious before the demo, not during."""

    def test_an_unauthenticated_config_reports_itself(self) -> None:
        self.assertFalse(config(customer_id=Secret(""), customer_secret=Secret("")).is_authenticated)
        self.assertTrue(config().is_authenticated)

    def test_credentials_never_render_in_a_repr(self) -> None:
        rendered = repr(config())
        self.assertNotIn("cust-secret", rendered)
        self.assertNotIn("cust-id", rendered)


if __name__ == "__main__":
    unittest.main()
