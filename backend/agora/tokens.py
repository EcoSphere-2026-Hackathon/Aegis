"""Server-only Agora credential issuance for one AEGIS voice participant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.common.config import AgoraConfig
from backend.common.errors import ConfigError


@dataclass(frozen=True)
class VoiceTokens:
    rtc_token: str
    rtm_token: str
    expires_at: datetime


def issue_voice_tokens(config: AgoraConfig, *, channel: str, uid: str) -> VoiceTokens:
    """Mint separate client RTC and RTM tokens bound to one numeric identity.

    The browser gets these scoped credentials only. The App Certificate stays
    in this process and is never serialized by an API response or log.
    """
    config.require_token_issuer()
    if not uid.isdigit() or int(uid) <= 0:
        raise ConfigError("voice participant uid must be a positive numeric string", uid=uid)
    if not channel:
        raise ConfigError("voice channel must not be empty")

    try:
        from agora_agent.agentkit.token import generate_convo_ai_token
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise ConfigError("agora-agents is required for RTC/RTM token issuance") from exc

    ttl = config.token_ttl_seconds
    numeric_uid = int(uid)
    app_certificate = config.app_certificate.reveal()
    # AccessToken2's ConvoAI token carries the RTC and RTM privileges for the
    # same app/channel/uid. Passing the same scoped value to both browser SDKs
    # matches Agora's official ConvoAI quickstart and prevents privilege drift
    # between two independently minted client tokens.
    combined = generate_convo_ai_token(
        config.app_id, app_certificate, channel, numeric_uid, ttl, ttl
    )
    return VoiceTokens(
        rtc_token=combined,
        rtm_token=combined,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    )
