"""In-process voice-session ownership; incident reasoning remains elsewhere."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backend.agora.client import AgoraClient, AgoraSpeechSink
from backend.agora.tokens import VoiceTokens, issue_voice_tokens
from backend.common.config import AgoraConfig
from backend.common.errors import AgoraError, InterventionError
from backend.common.logging import get_logger

_log = get_logger("agora.sessions")


@dataclass(frozen=True)
class VoiceSession:
    session_id: str
    incident_id: str
    participant_uid: str
    channel: str
    agent_id: str
    agent_uid: str
    expires_at: datetime


class VoiceSessionManager:
    """Owns agent IDs and prevents duplicate starts for one incident/user."""

    def __init__(self, config: AgoraConfig, *, client: Optional[AgoraClient] = None) -> None:
        self._config = config
        self._client = client or AgoraClient(config)
        self._owns_client = client is None
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str], VoiceSession] = {}
        self._by_id: dict[str, VoiceSession] = {}

    def start(self, *, incident_id: str, participant_uid: str) -> tuple[VoiceSession, VoiceTokens, bool]:
        key = (incident_id, participant_uid)
        with self._lock:
            existing = self._by_key.get(key)
            if existing:
                return existing, issue_voice_tokens(self._config, channel=existing.channel, uid=participant_uid), False
            channel = f"aegis-{incident_id}-{secrets.token_urlsafe(8)}"[:128]
            tokens = issue_voice_tokens(self._config, channel=channel, uid=participant_uid)
            agent_id = self._client.join(
                channel=channel,
                agent_uid=self._config.agent_uid,
                name=f"aegis-{secrets.token_hex(4)}",
            )
            session = VoiceSession(
                session_id=f"vs_{secrets.token_urlsafe(12)}",
                incident_id=incident_id,
                participant_uid=participant_uid,
                channel=channel,
                agent_id=agent_id,
                agent_uid=self._config.agent_uid,
                expires_at=tokens.expires_at,
            )
            self._by_key[key] = session
            self._by_id[session.session_id] = session
            return session, tokens, True

    def renew(self, session_id: str, participant_uid: str) -> tuple[VoiceSession, VoiceTokens]:
        session = self.get(session_id, participant_uid)
        return session, issue_voice_tokens(self._config, channel=session.channel, uid=participant_uid)

    def get(self, session_id: str, participant_uid: Optional[str] = None) -> VoiceSession:
        with self._lock:
            session = self._by_id.get(session_id)
        if not session or (participant_uid is not None and session.participant_uid != participant_uid):
            raise AgoraError("voice session was not found", session_id=session_id)
        return session

    def stop(self, session_id: str, participant_uid: Optional[str] = None) -> bool:
        with self._lock:
            session = self._by_id.get(session_id)
            if not session:
                return False
            if participant_uid is not None and session.participant_uid != participant_uid:
                raise AgoraError("voice session was not found", session_id=session_id)
            self._by_id.pop(session_id, None)
            self._by_key.pop((session.incident_id, session.participant_uid), None)
        try:
            self._client.leave(session.agent_id)
        except AgoraError:
            _log.warning("agent leave failed during voice-session cleanup", session_id=session_id)
        return True

    def speak(self, incident_id: str, text: str) -> None:
        with self._lock:
            candidates = [s for s in self._by_id.values() if s.incident_id == incident_id]
        if not candidates:
            raise InterventionError("no active Agora voice session", sink="agora")
        AgoraSpeechSink(self._client, candidates[0].agent_id).speak(text)

    def has_active_session(self, incident_id: str) -> bool:
        with self._lock:
            return any(session.incident_id == incident_id for session in self._by_id.values())

    def close(self) -> None:
        with self._lock:
            sessions = tuple(self._by_id.values())
            self._by_id.clear()
            self._by_key.clear()
        for session in sessions:
            try:
                self._client.leave(session.agent_id)
            except AgoraError:
                _log.warning("agent leave failed during shutdown", session_id=session.session_id)
        if self._owns_client:
            self._client.close()


class SessionAwareSpeechSink:
    """Uses Agora only for an active voice room; preserves demo recording otherwise."""

    name = "session-aware"

    def __init__(self, manager: VoiceSessionManager, incident_id: str, fallback) -> None:  # noqa: ANN001
        self._manager = manager
        self._incident_id = incident_id
        self._fallback = fallback

    def speak(self, text: str) -> None:
        if self._manager.has_active_session(self._incident_id):
            self._manager.speak(self._incident_id, text)
            return
        self._fallback.speak(text)
