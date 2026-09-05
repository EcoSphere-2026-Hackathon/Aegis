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
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Optional, Sequence

from pydantic import ValidationError

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.enums import ClaimType
from backend.common.errors import ProviderError, ProviderResponseError
from backend.common.logging import (
    STAGE_CLAIM_EXTRACTED,
    STAGE_CLAIM_REJECTED,
    get_logger,
)
from backend.common.metrics import (
    EXTRACTION_CACHE_HITS,
    EXTRACTION_FALLBACK_USED,
    EXTRACTION_FAST_PATH,
    EXTRACTION_PROVIDER_CALLS,
    EXTRACTION_REQUESTS,
    Metrics,
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

#: Utterances that are *entirely* backchannel. Anchored at both ends on
#: purpose: this must never match a prefix. "Okay" is filler; "Okay, roll Core
#: back" is a rollback, and a fast path that fired on the first word of it
#: would skip the most important claim in the incident.
#:
#: This is the cheapest optimisation in the system and one of the most
#: valuable. On a real bridge a large share of turns are acknowledgements, and
#: each one otherwise costs a network round trip, a model call and its tokens
#: to be told there is nothing there.
_BACKCHANNEL = re.compile(
    r"^(?:"
    r"(?:u?m+|a+h+|e+r+|hm+|mhm+|uh[- ]?huh|yeah|yep|yup|yes|no|nope|ok|okay|k|right|sure|"
    r"cool|nice|got it|gotcha|makes sense|sounds good|agreed|true|exactly|fair|fine|"
    r"thanks|thank you|cheers|hello|hi|hey|morning|sorry|what|huh|really|wow|ouch)"
    r"[\s,.!?]*"
    r")+$",
    re.IGNORECASE,
)

#: Only short utterances are cache-eligible. Long ones are where pronouns,
#: references and context live -- "roll it back" means different things in
#: different conversations, and a cache that ignored that would return a
#: confidently wrong claim. Short repeated phrases ("any update?", "still
#: there?", "go ahead") carry no such dependency and are exactly the ones
#: that recur.
CACHEABLE_MAX_WORDS = 8

#: Bounded so a long incident cannot grow the cache without limit.
EXTRACTION_CACHE_SIZE = 256


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
        metrics: Optional[Metrics] = None,
        cache_size: int = EXTRACTION_CACHE_SIZE,
        fallback_provider: Optional[ExtractionProvider] = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        #: Tried when the configured provider cannot produce usable claims.
        #: Not a retry -- a different, local, offline extractor.
        self._fallback = fallback_provider
        self._clock = clock
        self._max_attempts = max_attempts
        self._known_targets = tuple(known_targets)
        self._known_metrics = tuple(known_metrics)
        self._metric_aliases = dict(metric_aliases or {})
        self._metrics = metrics or Metrics()
        self._recent: deque[str] = deque(maxlen=context_turns)
        # Insertion-ordered so eviction is O(1) at the cold end; the hot end
        # is refreshed on every hit.
        # Extraction deliberately runs *outside* the pipeline lock so a slow
        # provider cannot stall state transitions. That makes this cache and
        # the rolling context reachable from more than one thread, so they
        # get their own lock -- held only around the dictionary operations,
        # never across a provider call.
        self._cache_lock = threading.Lock()
        self._cache: "OrderedDict[tuple, str]" = OrderedDict()
        self._cache_size = max(0, cache_size)

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
        self._metrics.increment(EXTRACTION_REQUESTS)

        # Fast path: a pure acknowledgement carries no claim, and asking a
        # model to confirm that costs a round trip on the critical path of a
        # system whose entire premise is speed.
        #
        # It is switched off the moment something is awaiting a decision. The
        # words are identical -- "yeah" is filler in open conversation and an
        # answer when AEGIS has just asked a question -- and the two cases are
        # only distinguishable by what is pending. An optimisation that can
        # silently discard a human's answer is not an optimisation; the
        # resolution policy, not a regex, is what decides whether that answer
        # can be safely applied.
        if not pending_action_targets and _BACKCHANNEL.match(event.text.strip()):
            self._metrics.increment(EXTRACTION_FAST_PATH)
            self._remember(event)
            _log.debug(
                "backchannel short-circuited before the provider",
                stage=STAGE_CLAIM_EXTRACTED,
                turn_id=event.turn_id,
                text=event.text[:40],
            )
            return ExtractionOutcome(
                claims=(self._none_claim(event),),
                provider="fast_path",
                model="backchannel",
                prompt_version=PROMPT_VERSION,
                attempts=0,
            )

        context = ExtractionContext(
            recent_turns=self._recent_turns(),
            known_targets=self._known_targets,
            known_metrics=self._known_metrics,
            metric_aliases=self._metric_aliases,
            pending_action_targets=tuple(pending_action_targets),
        )

        cache_key = self._cache_key(event, context)
        if cache_key is not None:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
            if cached is not None:
                self._metrics.increment(EXTRACTION_CACHE_HITS)
                # The cached value is the provider's *raw response*, not the
                # finished claims -- so it is re-parsed here and provenance is
                # taken from this event. A cached claim must never carry the
                # speaker or timestamp of the utterance that populated the
                # entry, or the decision ledger would attribute one person's
                # words to another.
                try:
                    claims, rejected = self._parse(cached, event)
                except ProviderResponseError:
                    with self._cache_lock:
                        self._cache.pop(cache_key, None)
                else:
                    self._remember(event)
                    return ExtractionOutcome(
                        claims=claims or (self._none_claim(event),),
                        rejected=rejected,
                        provider="cache",
                        model="cache",
                        prompt_version=PROMPT_VERSION,
                        attempts=0,
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
            self._metrics.increment(EXTRACTION_PROVIDER_CALLS)
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
            reason = failure or "provider_unavailable"
            recovered = self._fallback_outcome(event, request, attempts, elapsed_ms, reason)
            if recovered is not None:
                return recovered
            return self._degraded_outcome(event, attempts, elapsed_ms, reason)

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
            reason = f"{exc.code}: {exc.message}"
            # Malformed output is not retried by the loop above -- the call
            # succeeded, the content was garbage -- so the fallback is the
            # only thing standing between this and a lost utterance.
            recovered = self._fallback_outcome(event, request, attempts, elapsed_ms, reason)
            if recovered is not None:
                return recovered
            return self._degraded_outcome(event, attempts, elapsed_ms, reason)

        if not claims:
            claims = (self._none_claim(event),)

        if cache_key is not None and not rejected:
            # Only clean responses are cached. Storing one the validator
            # partially rejected would replay that defect on every future hit.
            self._store_in_cache(cache_key, response.raw_text)

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

    def _fallback_outcome(
        self,
        event: TranscriptEvent,
        request: ProviderRequest,
        attempts: int,
        elapsed_ms: float,
        reason: str,
    ) -> Optional[ExtractionOutcome]:
        """Extract locally when the configured provider could not.

        A hosted model that times out, rate-limits or returns garbage would
        otherwise cost the whole utterance: the loop survives, but it survives
        *blind*, and the utterance it dropped may have been the one proposing
        a destructive action. Since a complete offline extractor already ships
        in this repository, going blind is a choice rather than a constraint.

        Deliberately not a retry. The primary has already had its attempts;
        this is a different extractor with different failure modes, which is
        the only kind of fallback worth having.

        Three properties keep it honest:

        * **Same validation.** The result goes through the identical parse and
          vocabulary constraint as any other provider, so a fallback claim
          cannot reference a component the topology does not have.
        * **Never cached.** The cache stores what the *configured* provider
          said; seeding it from the fallback would serve degraded extractions
          long after the outage ended.
        * **Visible.** The provider name is recorded on the outcome, the
          failure that caused it is kept in ``failure_reason``, and it is
          counted separately -- so no run can silently pass as a live-model
          one.
        """
        if self._fallback is None:
            return None

        try:
            response = self._fallback.complete(request)
            claims, rejected = self._parse(response.raw_text, event)
        except Exception as exc:  # noqa: BLE001 - the fallback must not raise either
            _log.warning(
                "extraction fallback also failed",
                stage=STAGE_CLAIM_REJECTED,
                fallback=getattr(self._fallback, "name", "unknown"),
                failure_type=type(exc).__name__,
            )
            return None

        substantive = [claim for claim in claims if claim.type is not ClaimType.NONE]
        if not substantive:
            # Nothing was recovered, so report the outage rather than dressing
            # an empty result up as a successful extraction.
            return None

        self._metrics.increment(EXTRACTION_FALLBACK_USED)
        _log.warning(
            "extraction fell back to the local extractor",
            stage=STAGE_CLAIM_EXTRACTED,
            turn_id=event.turn_id,
            primary=getattr(self._provider, "name", "unknown"),
            fallback=getattr(self._fallback, "name", "unknown"),
            detail=reason,
            claim_types=[claim.type.value for claim in claims],
        )
        return ExtractionOutcome(
            claims=claims,
            rejected=rejected,
            provider=f"{getattr(self._fallback, 'name', 'fallback')}_fallback",
            model="local",
            prompt_version=PROMPT_VERSION,
            attempts=attempts,
            latency_ms=elapsed_ms,
            # Not degraded: claims were produced and the loop is not blind.
            # The reason the primary failed is kept so the run is auditable.
            degraded=False,
            failure_reason=reason,
        )

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
        """Drop metric references telemetry cannot serve.

        ``metric_ref`` is constrained because a metric the telemetry layer
        cannot read can never be grounded: the claimed value would sit in
        state forever with nothing to compare it against, looking checked.
        Dropping the reference makes that gap visible in the logs instead.

        ``target_ref`` is deliberately **not** constrained. Restricting it to
        the shipped topology stopped the model naming any component outside a
        ten-node fixture, which is too narrow for real speech. The cost of
        letting it through is that an action can now name something the
        dependency graph cannot locate -- so the honesty has to live where
        the consequence does. ``check_blast_radius`` reports an unlocatable
        target as ``unassessable_target`` rather than returning no findings,
        because the alternative is a destructive action shown as LOW with
        nothing against it, which reads as "assessed and safe" when it was
        never assessable at all.
        """
        if not (
            claim.metric_ref
            and self._known_metrics
            and claim.metric_ref not in self._known_metrics
        ):
            return claim

        _log.warning(
            "dropping unknown metric_ref from claim",
            stage=STAGE_CLAIM_REJECTED,
            metric_ref=claim.metric_ref,
            known_metrics=list(self._known_metrics),
        )
        return claim.model_copy(update={"metric_ref": None, "claimed_value": None})

    def _cache_key(self, event: TranscriptEvent, context: ExtractionContext) -> Optional[tuple]:
        """A key, or ``None`` when this utterance must not be cached.

        The pending-action set is part of the key because it changes what the
        same words mean: "yes, go ahead" is a confirmation only while
        something is awaiting a decision, and serving a cached confirmation
        when nothing is pending would be an authorisation bug rather than a
        stale-cache annoyance.
        """
        if self._cache_size <= 0:
            return None
        normalised = " ".join(event.text.lower().split())
        if not normalised or len(normalised.split()) > CACHEABLE_MAX_WORDS:
            return None
        # The *set* of pending targets is what changes the meaning of the
        # words; the order they happen to be listed in does not. Keying on
        # the ordered tuple would miss on every reordering and make the cache
        # decorative in exactly the long incidents it exists for.
        pending = tuple(sorted(set(context.pending_action_targets)))
        return (normalised, pending, event.source_modality.value)

    def _store_in_cache(self, key: tuple, raw_text: str) -> None:
        with self._cache_lock:
            self._cache[key] = raw_text
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    @property
    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._cache)

    @property
    def provider_name(self) -> str:
        """Which extractor is actually running.

        Surfaced on ``/api/health`` because "is the real model wired up or
        did it fall back?" is the first thing to check before a demo, and
        reaching into a private attribute to answer it is how that check
        breaks silently when the field is renamed.
        """
        return getattr(self._provider, "name", "unknown")

    def reset(self) -> None:
        """Drop the cache and the conversational context.

        Carrying the previous incident's last few turns into the next one
        gives the model context from a conversation that did not happen, and
        a cached answer keyed on a pending set that no longer exists.
        """
        with self._cache_lock:
            self._cache.clear()
            self._recent.clear()

    def _recent_turns(self) -> tuple[str, ...]:
        with self._cache_lock:
            return tuple(self._recent)

    def _remember(self, event: TranscriptEvent) -> None:
        with self._cache_lock:
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
