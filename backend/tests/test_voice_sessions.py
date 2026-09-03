"""Unit tests for the AEGIS-owned Agora voice-session lifecycle.

Written against ``unittest`` like every other suite here. The originals used
bare pytest functions, which ``python -m unittest discover`` -- the command
the README and CLAUDE.md both name as *the* test command -- does not collect:
they passed under pytest and were silently invisible to the documented run.
pytest is also not in ``requirements.txt``, so a clean checkout could not
execute them at all.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

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
class VoiceSessionLifecycleTests(unittest.TestCase):
    def test_start_is_idempotent_per_incident_and_participant(self, _issue) -> None:  # noqa: ANN001
        client = FakeAgoraClient()
        manager = VoiceSessionManager(config(), client=client)

        first, _tokens, created = manager.start(incident_id="inc-1", participant_uid="1001")
        second, _tokens, created_again = manager.start(incident_id="inc-1", participant_uid="1001")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(len(client.joins), 1)
        self.assertEqual(client.joins[0]["agent_uid"], "9000")

    def test_stop_requires_the_session_owner_and_is_idempotent(self, _issue) -> None:  # noqa: ANN001
        client = FakeAgoraClient()
        manager = VoiceSessionManager(config(), client=client)
        session, _tokens, _created = manager.start(incident_id="inc-1", participant_uid="1001")

        with self.assertRaises(AgoraError):
            manager.stop(session.session_id, participant_uid="1002")
        self.assertTrue(manager.stop(session.session_id, participant_uid="1001"))
        self.assertFalse(manager.stop(session.session_id, participant_uid="1001"))
        self.assertEqual(client.leaves, ["agent-1"])

    def test_a_session_id_is_unique_per_start(self, _issue) -> None:  # noqa: ANN001
        # The transcript relay scopes Agora's per-agent turn counter to this
        # id, so two participants -- and a rejoin -- must never share one, or
        # the second speaker's turns are dropped as duplicates downstream.
        client = FakeAgoraClient()
        manager = VoiceSessionManager(config(), client=client)

        alice, _t, _c = manager.start(incident_id="inc-1", participant_uid="1001")
        bob, _t, _c = manager.start(incident_id="inc-1", participant_uid="1002")
        self.assertNotEqual(alice.session_id, bob.session_id)
        self.assertNotEqual(alice.channel, bob.channel)

        manager.stop(alice.session_id, participant_uid="1001")
        rejoined, _t, created = manager.start(incident_id="inc-1", participant_uid="1001")
        self.assertTrue(created)
        self.assertNotEqual(rejoined.session_id, alice.session_id)
