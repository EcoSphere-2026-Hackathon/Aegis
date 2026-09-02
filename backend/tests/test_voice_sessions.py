"""Unit tests for the AEGIS-owned Agora voice-session lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from backend.agora.sessions import VoiceSessionManager
from backend.agora.tokens import VoiceTokens
from backend.common.config import AgoraConfig, Secret
from backend.common.errors import AgoraError


class FakeAgoraClient:
    def __init__(self) -> None:
        self.joins: list[dict] = []
        self.leaves: list[str] = []

    def join(self, **kwargs) -> str:  # noqa: ANN003
        self.joins.append(kwargs)
        return "agent-1"

    def leave(self, agent_id: str) -> None:
        self.leaves.append(agent_id)


def config() -> AgoraConfig:
    return AgoraConfig(
        app_id="app-id", channel_name="unused", customer_id=Secret("customer"),
        customer_secret=Secret("secret"), app_certificate=Secret("certificate"),
    )


def tokens(*_args, **_kwargs) -> VoiceTokens:  # noqa: ANN002, ANN003
    return VoiceTokens("rtc-token", "rtm-token", datetime.now(timezone.utc))


@patch("backend.agora.sessions.issue_voice_tokens", side_effect=tokens)
def test_start_is_idempotent_per_incident_and_participant(_issue) -> None:  # noqa: ANN001
    client = FakeAgoraClient()
    manager = VoiceSessionManager(config(), client=client)

    first, _tokens, created = manager.start(incident_id="inc-1", participant_uid="1001")
    second, _tokens, created_again = manager.start(incident_id="inc-1", participant_uid="1001")

    assert created is True
    assert created_again is False
    assert second.session_id == first.session_id
    assert len(client.joins) == 1
    assert client.joins[0]["agent_uid"] == "9000"


@patch("backend.agora.sessions.issue_voice_tokens", side_effect=tokens)
def test_stop_requires_the_session_owner_and_is_idempotent(_issue) -> None:  # noqa: ANN001
    client = FakeAgoraClient()
    manager = VoiceSessionManager(config(), client=client)
    session, _tokens, _created = manager.start(incident_id="inc-1", participant_uid="1001")

    with pytest.raises(AgoraError):
        manager.stop(session.session_id, participant_uid="1002")
    assert manager.stop(session.session_id, participant_uid="1001") is True
    assert manager.stop(session.session_id, participant_uid="1001") is False
    assert client.leaves == ["agent-1"]
