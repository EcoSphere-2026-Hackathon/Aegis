"""
The LLM Structured Extraction service.

Its whole job is to convert one utterance into typed claims. It holds no
incident state, decides no risk, and cannot block the ingestion loop: a
provider that hangs, errors, or returns nonsense degrades to a single
``type: none`` claim and the pipeline keeps moving (Blueprint §4 c2).

Three properties are enforced here rather than hoped for:

* **Provenance is never taken from the model.** ``speaker_uid``,
  ``timestamp``, ``source_turn_id`` and ``source_modality`` are copied from
  the transcript event. A model that hallucinates a different speaker cannot
  cause a claim to be attributed to the wrong human -- which matters because
  attribution feeds the decision ledger, and a misattributed confirmation is
  an authorisation failure.
* **Invalid output is rejected, counted, and survivable.** Malformed claims
  are dropped individually; the valid ones in the same response still land.
* **Retries are bounded.** Failure is a normal, logged outcome, not an
  exception that escapes into the pipeline.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Iterable, Optional, Sequence

from pydantic import ValidationError

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.enums import ClaimType, SourceModality
from backend.common.errors import ProviderError, ProviderResponseError
from backend.common.logging import (
    STAGE_CLAIM_EXTRACTED,
    STAGE_CLAIM_REJECTED,
    get_logger,
)
from backend.common.models import ExtractedClaim, TranscriptEvent
from backend.extraction.contracts import (
    ExtractionContext,
    ExtractionOutcome,
    ExtractionProvider,
    ProviderRequest,
    RejectedClaim,
)
from backend.extraction.prompt import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)

_log = get_logger("extraction")

#: How many prior utterances to carry as context. Enough to resolve "roll it
#: back" to a target named a few turns earlier, small enough that the prompt
#: stays cheap and the model is not tempted to re-extract old claims.
DEFAULT_CONTEXT_TURNS = 6

#: Fields the model is never allowed to determine.
_PROVENANCE_FIELDS = ("speaker_uid", "timestamp", "source_turn_id", "source_modality")


class ExtractionService:
    def __init__(
        self,
        provider: ExtractionProvider,
        *,
        clock: Clock = SYSTEM_CLOCK,
        max_attempts: int = 2,
        context_turns: int = DEFAULT_CONTEXT_TURNS,
        known_targets: Sequence[str] = (),
        known_metrics: Sequence[str] = (),
        metric_aliases: Optional[dict] = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        self._clock = clock
        self._max_attempts = max_attempts
        self._known_targets = tuple(known_targets)
        self._known_metrics = tuple(known_metrics)
        self._metric_aliases = dict(metric_aliases or {})
        self._recent: deque[str] = deque(maxlen=context_turns)

    # -- public API -------------------------------------------------------

    def extract(
        self,
        event: TranscriptEvent,
        *,
        pending_action_targets: Sequence[str] = (),
    ) -> ExtractionOutcome:
        """Extract claims from one final transcript event.

        Never raises. Every failure path returns an outcome carrying a
        ``type: none`` claim so the caller can distinguish "nothing was
        said" from "we could not hear", and the loop is never blocked.
        """
        context = ExtractionContext(
            recent_turns=tuple(self._recent),
            known_targets=self._known_targets,
            known_metrics=self._known_metrics,
            metric_aliases=self._metric_aliases,
            pending_action_targets=tuple(pending_action_targets),
        )
        request = ProviderRequest(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(event.text, event.uid, context),
            json_schema=RESPONSE_SCHEMA,
            prompt_version=PROMPT_VERSION,
            utterance=event.text,
            speaker_uid=event.uid,
            context=context,
        )

        started = time.perf_counter()
        response = None
        attempts = 0
        failure: Optional[str] = None

        while attempts < self._max_attempts:
            attempts += 1
            try:
                response = self._provider.complete(request)
                break
            except ProviderError as exc:
                failure = f"{exc.code}: {exc.message}"
                _log.warning(
                    "extraction provider call failed",
                    stage=STAGE_CLAIM_REJECTED,
                    attempt=attempts,
                    max_attempts=self._max_attempts,
                    provider=getattr(self._provider, "name", "unknown"),
                    failure_type=type(exc).__name__,
                    detail=exc.message,
                )
            except Exception as exc:  # noqa: BLE001 - a provider must never take the loop down
                failure = f"unexpected_provider_error: {exc!r}"
                _log.exception(
                    "extraction provider raised unexpectedly",
                    attempt=attempts,
                    provider=getattr(self._provider, "name", "unknown"),
                )

        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if response is None:
            self._remember(event)
            return self._degraded_outcome(event, attempts, elapsed_ms, failure or "provider_unavailable")

        try:
            claims, rejected = self._parse(response.raw_text, event)
        except ProviderResponseError as exc:
            self._remember(event)
            _log.warning(
                "extraction response was unusable",
                stage=STAGE_CLAIM_REJECTED,
                provider=response.provider,
                failure_type=type(exc).__name__,
                detail=exc.message,
            )
            return self._degraded_outcome(event, attempts, elapsed_ms, f"{exc.code}: {exc.message}")

        if not claims:
            claims = (self._none_claim(event),)

        self._remember(event)

        _log.info(
            "claims extracted",
            stage=STAGE_CLAIM_EXTRACTED,
            duration_ms=elapsed_ms,
            turn_id=event.turn_id,
            speaker_uid=event.uid,
            provider=response.provider,
            model=response.model,
            prompt_version=PROMPT_VERSION,
            attempts=attempts,
            claim_types=[claim.type.value for claim in claims],
            rejected_count=len(rejected),
        )

        return ExtractionOutcome(
            claims=claims,
            rejected=rejected,
            provider=response.provider,
            model=response.model,
            prompt_version=PROMPT_VERSION,
            attempts=attempts,
            latency_ms=elapsed_ms,
        )

    # -- internals --------------------------------------------------------

    def _parse(
        self, raw_text: str, event: TranscriptEvent
    ) -> tuple[tuple[ExtractedClaim, ...], tuple[RejectedClaim, ...]]:
        payload = _load_json_object(raw_text)
        raw_claims = payload.get("claims")
        if not isinstance(raw_claims, list):
            raise ProviderResponseError(
                "response did not contain a claims array", keys=sorted(payload.keys())
            )

        claims: list[ExtractedClaim] = []
        rejected: list[RejectedClaim] = []

        for raw_claim in raw_claims:
            if not isinstance(raw_claim, dict):
                rejected.append(RejectedClaim(raw=_clip(raw_claim), reason="claim was not an object"))
                continue
            try:
                claims.append(self._build_claim(raw_claim, event))
            except (ValidationError, ValueError) as exc:
                rejected.append(RejectedClaim(raw=_clip(raw_claim), reason=_first_error(exc)))
                _log.warning(
                    "claim rejected by validation",
                    stage=STAGE_CLAIM_REJECTED,
                    turn_id=event.turn_id,
                    reason=_first_error(exc),
                    raw=_clip(raw_claim),
                )

        return tuple(claims), tuple(rejected)

    def _build_claim(self, raw_claim: dict, event: TranscriptEvent) -> ExtractedClaim:
        """Build a validated claim, overriding provenance from the event.

        Anything the model said about who spoke or when is discarded before
        validation, not after -- so there is no window in which a
        hallucinated ``speaker_uid`` exists as a valid object.
        """
        payload: dict[str, Any] = {
            key: value for key, value in raw_claim.items() if key not in _PROVENANCE_FIELDS
        }
        payload["speaker_uid"] = event.uid
        payload["timestamp"] = event.timestamp
        payload["source_turn_id"] = event.turn_id
        payload["source_modality"] = event.source_modality

        claim = ExtractedClaim.model_validate(payload)
        return self._constrain_vocabulary(claim)

    def _constrain_vocabulary(self, claim: ExtractedClaim) -> ExtractedClaim:
        """Drop references to components and metrics that do not exist.

        A ``target_ref`` the topology has never heard of cannot be evaluated
        for blast radius, and a ``metric_ref`` telemetry cannot serve cannot
        be grounded. Keeping the invented name would produce a claim that
        silently never matches anything; dropping it makes the gap visible
        in the logs instead.
        """
        updates: dict[str, Any] = {}

        if claim.target_ref and self._known_targets and claim.target_ref not in self._known_targets:
            _log.warning(
                "dropping unknown target_ref from claim",
                stage=STAGE_CLAIM_REJECTED,
                target_ref=claim.target_ref,
                known_targets=list(self._known_targets),
            )
            updates["target_ref"] = None

        if claim.metric_ref and self._known_metrics and claim.metric_ref not in self._known_metrics:
            _log.warning(
                "dropping unknown metric_ref from claim",
                stage=STAGE_CLAIM_REJECTED,
                metric_ref=claim.metric_ref,
                known_metrics=list(self._known_metrics),
            )
            updates["metric_ref"] = None
            updates["claimed_value"] = None

        if not updates:
            return claim

        if updates.get("target_ref", claim.target_ref) is None and claim.type is ClaimType.PROPOSED_ACTION:
            # A proposed action without a resolvable target cannot be risk-
            # evaluated at all. Degrade it to a hypothesis rather than
            # inventing a target or dropping the utterance entirely.
            return claim.model_copy(update={**updates, "type": ClaimType.HYPOTHESIS,
                                            "action_kind": None, "target_schema_version": None,
                                            "target_ref": None})
        return claim.model_copy(update=updates)

    def _remember(self, event: TranscriptEvent) -> None:
        self._recent.append(f"{event.uid}: {event.text}")

    def _none_claim(self, event: TranscriptEvent) -> ExtractedClaim:
        return ExtractedClaim(
            type=ClaimType.NONE,
            text="",
            speaker_uid=event.uid,
            timestamp=event.timestamp,
            source_turn_id=event.turn_id,
            source_modality=event.source_modality,
        )

    def _degraded_outcome(
        self, event: TranscriptEvent, attempts: int, elapsed_ms: float, reason: str
    ) -> ExtractionOutcome:
        return ExtractionOutcome(
            claims=(self._none_claim(event),),
            provider=getattr(self._provider, "name", "unknown"),
            prompt_version=PROMPT_VERSION,
            attempts=attempts,
            latency_ms=elapsed_ms,
            degraded=True,
            failure_reason=reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_object(raw_text: str) -> dict:
    """Parse the model's response, tolerating the usual wrappers.

    Models fenced in ```json blocks or prefixed with a sentence are common
    enough that failing the whole utterance over it would be a self-inflicted
    reliability problem. Anything beyond that is a genuine protocol error.
    """
    text = raw_text.strip()
    if not text:
        raise ProviderResponseError("provider returned an empty response")

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ProviderResponseError("provider response was not JSON", sample=_clip(raw_text)) from None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise ProviderResponseError("provider response was not JSON", sample=_clip(raw_text)) from None

    if not isinstance(payload, dict):
        raise ProviderResponseError("provider response was not a JSON object", sample=_clip(raw_text))
    return payload


def _clip(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            location = ".".join(str(part) for part in errors[0].get("loc", ()))
            return f"{location or 'claim'}: {errors[0].get('msg', 'invalid')}"
    return str(exc)
