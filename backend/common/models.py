"""
AEGIS data contracts.

Every cross-component payload in the system is defined here as a Pydantic v2
model: validated at the boundary, immutable once constructed, and able to
emit its own JSON Schema (which is how the extraction service constrains the
LLM's structured output rather than hoping for well-formed JSON).

Two invariants this module exists to hold:

* **The four claim types never collapse.** ``Fact`` / ``Hypothesis`` /
  ``Decision`` / ``ProposedAction`` are separate types with separate fields
  and separate lifecycles (SSOT §10 forbids the collapse explicitly).
* **``Evidence`` is not a claim.** A claim is something a human *asserted*.
  Evidence is an *observation about the world*, sourced from telemetry or a
  submitted screenshot. Collapsing them would erase the distinction the
  product's core differentiation rests on (SSOT §29).

Nothing in this module decides anything. Risk determinations live only in
``backend.risk_engine``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Mapping, Optional, Sequence, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from backend.common.clock import utc_now
from backend.common.enums import (
    ActionKind,
    ClaimType,
    DecisionStance,
    EvidenceSource,
    EvidenceSourceType,
    ExtractionCertainty,
    GovernorAction,
    HypothesisStatus,
    InterventionOutcome,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
    SourceModality,
)
from backend.common.errors import VerdictContractError

# ---------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------


def _require_utc(moment: datetime) -> datetime:
    """Reject naive datetimes at the boundary.

    A naive timestamp compared against an aware one raises at runtime, and a
    naive timestamp serialised into SQLite silently breaks the timeline's
    lexicographic ordering. Neither failure is acceptable mid-incident, so
    both are made impossible here.
    """
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=4000)]
ShortStr = Annotated[str, Field(min_length=1, max_length=200)]


def new_id() -> str:
    return str(uuid.uuid4())


class _Frozen(BaseModel):
    """Internal models: immutable, strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class _FrozenTolerant(BaseModel):
    """Models parsed from an *external* producer (an LLM, an Agora payload).

    Tolerant about unknown fields -- a provider adding a field it wasn't
    asked for should not invalidate an otherwise-correct claim -- but strict
    about the fields it does define.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Transport contract -- Agora RTM (SSOT §9: VERIFIED schema, do not modify)
# ---------------------------------------------------------------------------


class TranscriptEvent(_FrozenTolerant):
    """One ASR transcript event as delivered over Agora RTM.

    Also the shape the typed-text side-channel wraps its input into, so text
    ingestion needs no second contract (SSOT §29).
    """

    uid: ShortStr
    turn_id: ShortStr
    role: ShortStr = "human"
    text: str = ""
    final: bool = False
    timestamp: UtcDatetime = Field(default_factory=utc_now)
    source_modality: SourceModality = SourceModality.VOICE

    @property
    def is_actionable(self) -> bool:
        """Only final, non-empty, human turns enter the extraction pipeline.

        Interim ASR results are explicitly ignored: acting on a partial
        utterance risks extracting a claim the speaker never finished making.
        """
        return self.final and bool(self.text.strip()) and self.role == "human"


# ---------------------------------------------------------------------------
# Extraction contract
# ---------------------------------------------------------------------------


class ExtractedClaim(_FrozenTolerant):
    """The LLM extraction service's output contract (Blueprint §4 c2).

    The required field set is exactly the frozen one. The additional fields
    below are all **optional**, and each exists because a locked demo beat
    or a locked safety boundary cannot be implemented without it:

    ``action_kind`` / ``target_schema_version``
        SSOT §20 beat 6 requires naming the *rollback target schema*
        ("v2.3") and knowing the action is a rollback at all. Without these
        the blast-radius check has nothing structural to compare.
    ``decision_stance``
        Lets ``decision_reversal_check`` compare typed data instead of
        scanning decision prose for negation words. Natural-language
        interpretation is the LLM's job; the deterministic engine only
        compares fields (SSOT §7).
    ``claimed_value`` / ``claimed_unit``
        SSOT §20 beat 3 requires contrasting a spoken figure ("like 40%")
        against a measured one (91%).
    """

    claim_id: str = Field(default_factory=new_id)
    type: ClaimType
    text: str = ""
    speaker_uid: ShortStr
    timestamp: UtcDatetime
    source_turn_id: ShortStr

    target_ref: Optional[ShortStr] = None
    metric_ref: Optional[ShortStr] = None
    ownership_tag: Optional[ShortStr] = None
    source_modality: SourceModality = SourceModality.VOICE

    action_kind: Optional[ActionKind] = None
    target_schema_version: Optional[ShortStr] = None
    decision_stance: Optional[DecisionStance] = None
    claimed_value: Optional[float] = None
    claimed_unit: Optional[ShortStr] = None

    @model_validator(mode="after")
    def _check_conditional_requirements(self) -> "ExtractedClaim":
        if self.type is ClaimType.PROPOSED_ACTION and not self.target_ref:
            raise ValueError("a proposed_action claim requires target_ref")
        if self.type is not ClaimType.NONE and not self.text.strip():
            raise ValueError(f"a {self.type.value} claim requires non-empty text")
        if self.claimed_value is not None and self.metric_ref is None:
            raise ValueError("claimed_value requires metric_ref naming the metric claimed")
        return self


# ---------------------------------------------------------------------------
# Incident state entities (SSOT §8)
# ---------------------------------------------------------------------------


class Fact(_Frozen):
    """Something asserted as settled. Facts do not go stale in this model."""

    claim_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    speaker_uid: ShortStr
    timestamp: UtcDatetime
    source_turn_id: Optional[ShortStr] = None
    source_modality: SourceModality = SourceModality.VOICE


class Hypothesis(_Frozen):
    """A hedge -- explicitly *not* a fact, and tracked with the reinforcement
    and staleness history that lets the engine tell a live theory from an
    abandoned one."""

    claim_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    speaker_uid: ShortStr
    timestamp: UtcDatetime
    status: HypothesisStatus = HypothesisStatus.ACTIVE
    reinforcement_count: int = Field(default=0, ge=0)
    last_touched_at: Optional[UtcDatetime] = None
    target_ref: Optional[ShortStr] = None
    metric_ref: Optional[ShortStr] = None
    claimed_value: Optional[float] = None
    claimed_unit: Optional[ShortStr] = None
    source_turn_id: Optional[ShortStr] = None
    source_modality: SourceModality = SourceModality.VOICE

    @model_validator(mode="after")
    def _default_last_touched(self) -> "Hypothesis":
        # A hypothesis nobody has revisited was last touched when it was
        # stated. Modelled explicitly rather than left null so the staleness
        # rules never have to special-case a missing timestamp.
        if self.last_touched_at is None:
            object.__setattr__(self, "last_touched_at", self.timestamp)
        return self

    @property
    def touched_at(self) -> datetime:
        return self.last_touched_at or self.timestamp

    @property
    def is_active(self) -> bool:
        return self.status is HypothesisStatus.ACTIVE


class Decision(_Frozen):
    """An entry in the append-only Decision Ledger."""

    claim_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    speaker_uid: ShortStr
    timestamp: UtcDatetime
    target_ref: Optional[ShortStr] = None
    stance: Optional[DecisionStance] = None
    source_turn_id: Optional[ShortStr] = None
    source_modality: SourceModality = SourceModality.VOICE


class ProposedAction(_Frozen):
    """A consequential operation someone proposed out loud.

    Starts ``pending`` and **only** leaves that state through an explicit,
    classified human resolution. Silence, ambiguity and timeouts all leave it
    pending, forever if necessary (Quality Standard §4 red line #1).
    """

    claim_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    target_ref: ShortStr
    speaker_uid: ShortStr
    timestamp: UtcDatetime
    action_kind: ActionKind = ActionKind.OTHER
    target_schema_version: Optional[ShortStr] = None
    status: ProposedActionStatus = ProposedActionStatus.PENDING
    risk_verdict: Optional["RiskVerdict"] = None
    resolved_by_uid: Optional[ShortStr] = None
    resolved_at: Optional[UtcDatetime] = None
    justifying_hypothesis_id: Optional[str] = None
    source_turn_id: Optional[ShortStr] = None
    source_modality: SourceModality = SourceModality.VOICE

    @model_validator(mode="after")
    def _check_resolution_consistency(self) -> "ProposedAction":
        if self.status.is_terminal:
            if not self.resolved_by_uid or self.resolved_at is None:
                raise ValueError(
                    "a resolved proposed_action must record who resolved it and when"
                )
        else:
            if self.resolved_by_uid or self.resolved_at is not None:
                raise ValueError("a pending proposed_action must not carry resolution metadata")
        return self


class Evidence(_Frozen):
    """An observation about the state of the world.

    Sourced either from the mocked telemetry endpoint or from a screenshot a
    participant submitted. Deliberately *not* a claim type (SSOT §29).
    """

    evidence_id: str = Field(default_factory=new_id)
    source_type: EvidenceSourceType
    source: EvidenceSource
    metric_name: ShortStr
    value: Union[float, str]
    unit: Optional[ShortStr] = None
    extraction_certainty: ExtractionCertainty = ExtractionCertainty.HIGH
    uploader_uid: Optional[ShortStr] = None
    timestamp: UtcDatetime
    target_ref: Optional[ShortStr] = None
    raw_reference: Optional[str] = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _check_source_consistency(self) -> "Evidence":
        if self.source is EvidenceSource.SCREENSHOT_UPLOAD and not self.uploader_uid:
            raise ValueError("screenshot-sourced evidence must record its uploader_uid")
        if self.source is EvidenceSource.MOCK_TELEMETRY:
            if self.extraction_certainty is not ExtractionCertainty.HIGH:
                raise ValueError("telemetry-sourced evidence is always high certainty")
            if self.uploader_uid is not None:
                raise ValueError("telemetry-sourced evidence has no uploader")
        return self

    @property
    def numeric_value(self) -> Optional[float]:
        """The reading as a number, or ``None`` if it is not numeric.

        Non-numeric readings are legitimate (``schema_version: "v17"``); the
        engine simply compares them as strings instead.
        """
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            return float(self.value)
        try:
            return float(str(self.value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Risk engine output
# ---------------------------------------------------------------------------


class RiskFinding(_Frozen):
    """One specific problem a single check found.

    ``message`` is the sentence AEGIS may say out loud; ``code`` is the same
    information in a form the evaluation harness, the UI and the structured
    logs can assert on without string-matching prose.
    """

    code: RiskFindingCode
    tier: RiskTier
    message: NonEmptyStr
    subject_claim_id: Optional[str] = None
    related_ids: tuple[str, ...] = ()
    detail: Mapping[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("tier")
    @classmethod
    def _findings_are_never_low(cls, tier: RiskTier) -> RiskTier:
        # A finding is by definition something worth reporting. "Nothing
        # found" is the absence of a finding, not a LOW-tier one.
        if tier is RiskTier.LOW:
            raise ValueError("a RiskFinding cannot be LOW tier; omit the finding instead")
        return tier

    @property
    def dedupe_key(self) -> tuple[str, ...]:
        """What makes two findings "the same problem", across evaluations.

        Deliberately *semantic* rather than literal. A metric re-read thirty
        seconds later produces a new ``Evidence`` id for the same
        disagreement about the same number; keying on that id would let
        AEGIS announce the identical contradiction again and again, spending
        one rate-limited intervention each time. Keying on the metric makes
        the second announcement recognisably redundant.

        Message text is not part of the key either: the same problem can be
        phrased differently between evaluations (a version resolved from the
        topology rather than from speech), and a text-based key would miss
        the match.
        """
        metric = self.detail.get("metric") if self.detail else None
        if metric:
            return (self.code.value, "metric", str(metric))
        return (self.code.value, *sorted(self.related_ids))


class RiskVerdict(_Frozen):
    """``risk_engine.evaluate()``'s output contract.

    ``reasons`` is the frozen field the rest of the system consumes; it is
    derived from ``findings`` so the two can never disagree.
    """

    # ``reasons`` is a computed field, so it appears in serialised output
    # (the API and the UI both want it) but is not an input. Round-tripping
    # therefore feeds it straight back in on parse, which a strict model
    # would reject -- hence ``extra="ignore"`` here specifically. The fields
    # that carry meaning are still validated strictly, and ``reasons`` is
    # always recomputed from ``findings`` rather than trusted from input, so
    # the two can never disagree.
    model_config = ConfigDict(frozen=True, extra="ignore")

    risk_tier: RiskTier
    findings: tuple[RiskFinding, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(finding.message for finding in self.findings)

    @property
    def codes(self) -> tuple[RiskFindingCode, ...]:
        return tuple(finding.code for finding in self.findings)

    @model_validator(mode="after")
    def _check_verdict_contract(self) -> "RiskVerdict":
        if self.risk_tier is not RiskTier.LOW and not self.findings:
            raise VerdictContractError(
                "a non-LOW verdict must carry at least one finding",
                risk_tier=self.risk_tier.value,
            )
        if self.risk_tier is RiskTier.LOW and self.findings:
            raise VerdictContractError(
                "a LOW verdict must carry no findings",
                finding_codes=[f.code.value for f in self.findings],
            )
        expected = RiskTier.max(*(finding.tier for finding in self.findings))
        if self.findings and expected is not self.risk_tier:
            raise VerdictContractError(
                "verdict tier must equal the highest finding tier",
                declared=self.risk_tier.value,
                expected=expected.value,
            )
        return self

    @classmethod
    def from_findings(cls, findings: Sequence[RiskFinding]) -> "RiskVerdict":
        ordered = tuple(sorted(findings, key=lambda f: (-f.tier.rank, f.code.value)))
        return cls(risk_tier=RiskTier.max(*(f.tier for f in ordered)), findings=ordered)

    @classmethod
    def low(cls) -> "RiskVerdict":
        return cls(risk_tier=RiskTier.LOW, findings=())


# Resolve the forward reference now that RiskVerdict exists.
ProposedAction.model_rebuild()


# ---------------------------------------------------------------------------
# Engine input contract
# ---------------------------------------------------------------------------


class StateSnapshot(_Frozen):
    """An immutable, point-in-time view of the incident state.

    This is the ``state`` parameter of the frozen engine signature
    ``evaluate(proposed_action, state, topology, evidence)``. It is captured
    inside a single read transaction, so the engine can never observe a state
    torn across a concurrent write.

    Evidence is deliberately *not* part of this snapshot: it is the engine's
    separate fourth parameter, exactly as the frozen contract specifies.
    """

    captured_at: UtcDatetime
    facts: tuple[Fact, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    decisions: tuple[Decision, ...] = ()
    proposed_actions: tuple[ProposedAction, ...] = ()

    def hypothesis(self, claim_id: Optional[str]) -> Optional[Hypothesis]:
        if claim_id is None:
            return None
        for hypothesis in self.hypotheses:
            if hypothesis.claim_id == claim_id:
                return hypothesis
        return None

    def active_hypotheses(self) -> tuple[Hypothesis, ...]:
        return tuple(h for h in self.hypotheses if h.is_active)

    def decisions_for(self, target_ref: Optional[str]) -> tuple[Decision, ...]:
        if target_ref is None:
            return ()
        return tuple(d for d in self.decisions if d.target_ref == target_ref)

    def pending_actions(self) -> tuple[ProposedAction, ...]:
        return tuple(a for a in self.proposed_actions if a.status is ProposedActionStatus.PENDING)


# ---------------------------------------------------------------------------
# Intervention records (observability + UI)
# ---------------------------------------------------------------------------


class InterventionRecord(_Frozen):
    """What the Governor decided, why, and whether it reached the humans.

    Persisted so a rehearsal can be reconstructed from logs alone, which the
    observability standard requires (Quality Standard §14).
    """

    intervention_id: str = Field(default_factory=new_id)
    action: GovernorAction
    outcome: InterventionOutcome
    risk_tier: RiskTier
    reasons: tuple[str, ...] = ()
    codes: tuple[RiskFindingCode, ...] = ()
    spoken_text: Optional[str] = None
    subject_claim_id: Optional[str] = None
    decided_at: UtcDatetime
    rate_limit_window_open: bool = True
    seconds_since_last_spoken: Optional[float] = None
    delivery_error: Optional[str] = None


class TimelineEntry(_Frozen):
    entry_id: str
    collection: str
    occurred_at: UtcDatetime
    summary: str
    speaker_uid: Optional[str] = None


class IncidentView(_Frozen):
    """The presentation-layer projection: everything the UI and the spoken
    status summary read from. Separate from ``StateSnapshot`` so the engine's
    input contract never drifts to satisfy a UI need."""

    incident_id: str
    captured_at: UtcDatetime
    facts: tuple[Fact, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    decisions: tuple[Decision, ...] = ()
    proposed_actions: tuple[ProposedAction, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    interventions: tuple[InterventionRecord, ...] = ()
    timeline: tuple[TimelineEntry, ...] = ()
