"""
Extraction service contracts.

The provider boundary is a ``Protocol`` rather than a base class so the real
vendor client, the offline rule-based extractor and any test double are
interchangeable without inheritance, and so the service can be tested with
no network at all.

Everything here is synchronous by design. The pipeline runs on a dedicated
worker thread and the HTTP layer is the only async surface in AEGIS; keeping
extraction sync avoids colouring the whole reasoning core async for no gain
at this request volume.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from backend.common.models import ExtractedClaim


class ExtractionContext(BaseModel):
    """What the extractor knows besides the utterance itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recent_turns: tuple[str, ...] = ()
    """Recent utterances, oldest first, rendered as ``uid: text``. Gives the
    model enough context to resolve "roll it back" to a target named three
    turns ago."""

    known_targets: tuple[str, ...] = ()
    """Service names from the topology. Constrains ``target_ref`` to a real
    vocabulary instead of letting the model invent component names the risk
    engine cannot match."""

    known_metrics: tuple[str, ...] = ()
    """Metric names the telemetry layer can actually serve."""

    metric_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """Metric name -> the phrases people use for it. People say "the pool",
    not "pool_utilization"; without this the claim never binds to a metric
    and can never be grounded against a reading."""

    pending_action_targets: tuple[str, ...] = ()
    """Targets currently awaiting a human decision -- context the model needs
    to recognise that "yeah, go ahead" is a confirmation rather than chatter."""


class ProviderRequest(BaseModel):
    """One structured-output call, transport-independent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: str
    user_prompt: str
    json_schema: dict
    prompt_version: str
    utterance: str
    speaker_uid: str
    context: ExtractionContext
    max_output_tokens: int = 900
    # ``utterance``/``speaker_uid``/``context`` carry the same information the
    # rendered prompts do, in structured form. Network providers ignore them
    # and send the prompts; the offline provider reads them instead of
    # re-parsing English out of a prompt string it just helped build.


class ProviderResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_text: str
    model: str
    provider: str
    latency_ms: float = 0.0


@runtime_checkable
class ExtractionProvider(Protocol):
    """Anything that can turn a prompt into raw structured text.

    Deliberately narrow: a provider does not validate, retry, or know what a
    claim is. It performs one call and returns what came back. All policy
    lives in :class:`~backend.extraction.service.ExtractionService`, so
    swapping vendors cannot change AEGIS's behaviour.
    """

    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        ...

    def supports_vision(self) -> bool:
        ...


class RejectedClaim(BaseModel):
    """A claim the model produced that did not survive validation.

    Kept rather than discarded: the rejection *rate* is a Phase 2 evaluation
    metric ("invalid structured-output rate"), and a claim silently dropped
    is a claim nobody can debug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: str
    reason: str


class ExtractionOutcome(BaseModel):
    """Everything one extraction attempt produced.

    ``claims`` may legitimately be a single ``type: none`` claim. That is a
    real result -- "heard, nothing extractable" -- and is distinct from
    ``degraded``, which means the provider failed and the pipeline is
    continuing blind rather than blocking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[ExtractedClaim, ...] = ()
    rejected: tuple[RejectedClaim, ...] = ()
    provider: str = "unknown"
    model: str = "unknown"
    prompt_version: str = "unknown"
    attempts: int = 0
    latency_ms: float = 0.0
    degraded: bool = False
    failure_reason: Optional[str] = None

    @property
    def substantive_claims(self) -> tuple[ExtractedClaim, ...]:
        from backend.common.enums import ClaimType

        return tuple(claim for claim in self.claims if claim.type is not ClaimType.NONE)
