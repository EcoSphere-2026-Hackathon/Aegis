"""
Agora Conversational AI Engine client.

Covers the three calls AEGIS makes: start an agent in the channel, make it
speak, and remove it. Everything else about Agora -- audio transport, ASR,
TTS, per-speaker attribution -- happens inside Agora and reaches us as RTM
transcript events relayed by the browser client.

**Verification status matters here and is not glossed over.** The SSOT marks
the request *schemas* as verified against documentation, and the live
*behaviour* as unverified: whether ``priority: INTERRUPT`` actually cuts
through a human who is mid-sentence, and whether manual turn detection
really keeps the agent silent, are open questions that only the live spike
can answer. This client implements the documented contracts and makes the
unverified parts configurable rather than hard-coded, so a spike result can
be absorbed by changing configuration instead of rewriting the integration.

Two hard rules:

* **Secrets never reach a log.** Credentials live in :class:`Secret`, the
  Authorization header is built at call time, and error contexts carry
  status codes rather than response bodies (a body can echo the request).
* **This client cannot execute anything.** Its entire outbound vocabulary is
  "join a call", "say this", "leave". There is no code path here through
  which AEGIS could act on the systems being discussed.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Optional

import httpx

from backend.common.config import AgoraConfig
from backend.common.errors import AgoraAuthError, AgoraError, InterventionError
from backend.common.logging import STAGE_SPEAK_CALLED, get_logger

_log = get_logger("agora")

#: Documented ``speak`` limit. Enforced client-side so an over-long
#: intervention fails here, loudly, rather than being silently truncated by
#: the service mid-reason.
SPEAK_MAX_BYTES = 512


class SpeakPriority:
    """Documented values. ``IGNORE`` appears only in a local skill file and
    is not corroborated by the official docs, so it is deliberately absent:
    nothing load-bearing should depend on an unverified enum value."""

    INTERRUPT = "INTERRUPT"
    APPEND = "APPEND"


class AgoraClient:
    def __init__(self, config: AgoraConfig, *, client: Optional[httpx.Client] = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.request_timeout_seconds),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AgoraClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- auth -------------------------------------------------------------

    def _auth_header(self) -> dict[str, str]:
        """Basic Auth from the Customer ID / Customer Secret pair.

        Not the App ID and App Certificate -- those authenticate RTC tokens,
        not the REST API, and using them here fails with a 401 that looks
        like a credential typo. The distinction is encoded in the config
        field names for the same reason.
        """
        self._config.require_auth()
        raw = f"{self._config.customer_id.reveal()}:{self._config.customer_secret.reveal()}"
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}

    # -- calls ------------------------------------------------------------

    def join(
        self,
        *,
        channel: Optional[str] = None,
        agent_uid: Optional[str] = None,
        name: str = "aegis",
        token: Optional[str] = None,
        silent_by_default: bool = True,
        extra_properties: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Put the AEGIS agent into the channel. Returns the agent id."""
        payload: dict[str, Any] = {
            "name": name,
            "properties": {
                "channel": channel or self._config.channel_name,
                "agent_rtc_uid": agent_uid or self._config.agent_uid,
                "remote_rtc_uids": ["*"],
                "enable_string_uid": False,
                "idle_timeout": 600,
                "advanced_features": {"enable_rtm": True},
                "parameters": {
                    "data_channel": "rtm",
                    "enable_metrics": self._config.enable_metrics,
                },
            },
        }

        if self._config.pipeline_id:
            payload["pipeline_id"] = self._config.pipeline_id

        if token:
            payload["properties"]["token"] = token

        if extra_properties:
            payload["properties"].update(dict(extra_properties))

        body = self._request(
            "POST",
            f"/api/conversational-ai-agent/v2/projects/{self._config.app_id}/join",
            payload,
            what="join",
        )

        agent_id = body.get("agent_id") or body.get("agentId")
        if not agent_id:
            raise AgoraError("join succeeded but returned no agent id", keys=sorted(body.keys()))

        _log.info(
            "agora agent joined",
            agent_id=agent_id,
            channel=channel or self._config.channel_name,
            metrics_enabled=self._config.enable_metrics,
            silent_by_default=silent_by_default,
        )
        return str(agent_id)

    def speak(
        self,
        agent_id: str,
        text: str,
        *,
        priority: str = SpeakPriority.INTERRUPT,
        interruptable: bool = False,
    ) -> None:
        """Say something in the channel.

        ``interruptable=False`` by default: an intervention that a human can
        talk over halfway through is an intervention that may deliver
        "rolling back Core will break" and stop -- which is worse than
        useless, because it names a risk without naming its consequence.
        """
        encoded_length = len(text.encode("utf-8"))
        if encoded_length > SPEAK_MAX_BYTES:
            raise InterventionError(
                "intervention exceeds the speak byte limit",
                bytes=encoded_length,
                max_bytes=SPEAK_MAX_BYTES,
            )
        if priority not in {SpeakPriority.INTERRUPT, SpeakPriority.APPEND}:
            raise InterventionError("unsupported speak priority", priority=priority)

        self._request(
            "POST",
            f"/api/conversational-ai-agent/v2/projects/{self._config.app_id}/agents/{agent_id}/speak",
            {"text": text, "priority": priority, "interruptable": interruptable},
            what="speak",
        )
        _log.info(
            "agora speak delivered",
            stage=STAGE_SPEAK_CALLED,
            agent_id=agent_id,
            priority=priority,
            bytes=encoded_length,
        )

    def leave(self, agent_id: str) -> None:
        self._request(
            "POST",
            f"/api/conversational-ai-agent/v2/projects/{self._config.app_id}/agents/{agent_id}/leave",
            {},
            what="leave",
        )
        _log.info("agora agent left", agent_id=agent_id)

    # -- transport --------------------------------------------------------

    def _request(self, method: str, path: str, payload: Mapping[str, Any], *, what: str) -> dict:
        try:
            response = self._client.request(method, path, json=payload, headers=self._auth_header())
        except httpx.TimeoutException as exc:
            raise AgoraError(
                f"agora {what} timed out",
                timeout_seconds=self._config.request_timeout_seconds,
                operation=what,
            ) from exc
        except httpx.HTTPError as exc:
            raise AgoraError(f"agora {what} transport error", operation=what) from exc

        if response.status_code in (401, 403):
            raise AgoraAuthError(
                "agora rejected the credentials -- check the Customer ID/Secret pair "
                "in Console → Developer Toolkit → RESTful API",
                operation=what,
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            # Status code only. A response body can echo the request, and the
            # request carries an Authorization header.
            raise AgoraError(
                f"agora {what} failed", operation=what, status_code=response.status_code
            )

        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}


class AgoraSpeechSink:
    """Delivers interventions as live audio through a joined agent.

    Holds the agent id rather than taking it per call, so the pipeline never
    has to know anything about Agora. A failure raises
    :class:`InterventionError`, which the pipeline treats as "decided but not
    heard" -- returning the rate-limit window instead of silently swallowing
    the intervention.
    """

    name = "agora"

    def __init__(self, client: AgoraClient, agent_id: str) -> None:
        self._client = client
        self._agent_id = agent_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def speak(self, text: str) -> None:
        try:
            self._client.speak(self._agent_id, text)
        except InterventionError:
            raise
        except AgoraError as exc:
            raise InterventionError(
                "agora speak failed", sink=self.name, detail=exc.message, **dict(exc.context)
            ) from exc
