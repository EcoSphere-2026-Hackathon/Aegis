"""
Typed exception hierarchy.

Two rules this file exists to enforce:

1. Nothing in AEGIS raises a bare ``Exception`` or asserts an invariant that
   matters. ``assert`` is stripped under ``python -O``; a safety invariant
   guarded by an assert is a safety invariant that can be compiled away.
2. Every failure carries enough structure for the observability layer to
   tag it by *type* (Quality Standard §14: "distinguishing failure types,
   not just a generic error log").
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class AegisError(Exception):
    """Base class for every error AEGIS raises deliberately."""

    #: Stable, machine-readable identifier used in structured logs and in
    #: API error envelopes. Subclasses override.
    code: str = "aegis_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, Any] = dict(context)

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({rendered})"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": dict(self.context)}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class ConfigError(AegisError):
    """Invalid or missing configuration. Raised at startup, never mid-run."""

    code = "config_error"


# --------------------------------------------------------------------------
# Contract / validation
# --------------------------------------------------------------------------

class ContractError(AegisError):
    """A value violated an AEGIS data contract."""

    code = "contract_error"


class ClaimValidationError(ContractError):
    """An extracted claim failed schema or conditional-field validation.

    Per the Blueprint's error-handling contract this is *logged and
    rejected*, never crashed on, and never forwarded to the State Store.
    """

    code = "claim_validation_error"


class VerdictContractError(ContractError):
    """A ``RiskVerdict`` violated its own contract -- e.g. a non-LOW tier
    with an empty ``reasons`` list. This is a programming error in the risk
    engine and must fail loudly rather than silently produce an
    unexplainable intervention."""

    code = "verdict_contract_error"


# --------------------------------------------------------------------------
# State store
# --------------------------------------------------------------------------

class StateStoreError(AegisError):
    code = "state_store_error"


class EntityNotFoundError(StateStoreError):
    """A mutation targeted an entity that does not exist. Silently no-oping
    here would let a resolution for a non-existent action 'succeed'."""

    code = "entity_not_found"


class IllegalStateTransitionError(StateStoreError):
    """A mutation attempted a transition the entity's state machine forbids.

    The load-bearing case: re-resolving a ``proposed_action`` that a human
    already resolved. Allowing it would silently change a prior human
    decision -- Quality Standard §4 red line #5.
    """

    code = "illegal_state_transition"


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

class ExtractionError(AegisError):
    code = "extraction_error"


class ProviderError(ExtractionError):
    """The upstream LLM provider failed, timed out, or returned a transport
    error. Retried under a bounded policy, then degraded to ``type: none``
    so the ingestion loop is never blocked."""

    code = "provider_error"


class ProviderResponseError(ExtractionError):
    """The provider responded, but the payload was not usable (not JSON, or
    JSON that does not conform to the extraction schema)."""

    code = "provider_response_error"


# --------------------------------------------------------------------------
# Intervention delivery
# --------------------------------------------------------------------------

class InterventionError(AegisError):
    code = "intervention_error"


class SpeechTooLongError(InterventionError):
    """Agora's ``speak`` endpoint caps ``text`` at 512 bytes. Truncating
    silently would cut an intervention off mid-reason, so the speech builder
    budgets explicitly instead."""

    code = "speech_too_long"


class AgoraError(AegisError):
    """A call to the Agora Conversational AI Engine failed."""

    code = "agora_error"


class AgoraAuthError(AgoraError):
    """Basic Auth against Agora was rejected -- almost always a missing or
    wrong Customer ID / Customer Secret pair."""

    code = "agora_auth_error"


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------

class ApiError(AegisError):
    code = "api_error"
    http_status: int = 400


class UnauthorizedError(ApiError):
    code = "unauthorized"
    http_status = 401


class RateLimitedError(ApiError):
    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.retry_after_seconds = retry_after_seconds


class PayloadTooLargeError(ApiError):
    code = "payload_too_large"
    http_status = 413
