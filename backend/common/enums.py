"""
All AEGIS enumerations, in one place.

Kept separate from ``models.py`` so that every layer (extraction, engine,
store, API, UI) shares one vocabulary and no module redefines a literal.

Python 3.10 target: ``StrEnum`` (3.11+) is deliberately not used.
"""

from __future__ import annotations

from enum import Enum


class ClaimType(str, Enum):
    """The typed output of the LLM extraction service (Blueprint §4 c2).

    ``NONE`` is emitted explicitly rather than by omitting output, so that
    "heard, nothing extractable" is distinguishable in the logs from
    "extraction failed".
    """

    FACT = "fact"
    HYPOTHESIS = "hypothesis"
    DECISION = "decision"
    PROPOSED_ACTION = "proposed_action"
    CONFIRMATION = "confirmation"
    OVERRIDE = "override"
    HOLD = "hold"
    NONE = "none"

    @property
    def is_resolution(self) -> bool:
        """Does this claim type resolve a pending proposed action?"""
        return self in _RESOLUTION_CLAIM_TYPES


_RESOLUTION_CLAIM_TYPES = frozenset(
    {ClaimType.CONFIRMATION, ClaimType.OVERRIDE, ClaimType.HOLD}
)


class SourceModality(str, Enum):
    """Where a *claim* came from.

    There is deliberately no ``IMAGE`` member: a screenshot does not produce
    an ``ExtractedClaim``, it produces an ``Evidence`` object (SSOT §29 --
    Evidence is a distinct entity, not a fifth claim type).
    """

    VOICE = "voice"
    TEXT = "text"


class DecisionStance(str, Enum):
    """The polarity of a logged decision, as classified by the extraction
    service.

    This exists so that ``decision_reversal_check`` can compare *structured
    data* rather than scanning decision prose for negation words. Natural-
    language interpretation belongs to the LLM; the deterministic engine
    only compares typed fields (SSOT §7 boundary).
    """

    HOLD = "hold"
    """The team decided NOT to proceed with the referenced target."""

    PROCEED = "proceed"
    """The team decided TO proceed with the referenced target."""


class ActionKind(str, Enum):
    """The kind of operation a ``proposed_action`` describes.

    Lets the engine decide *structurally* which checks are applicable (a
    schema-compatibility blast-radius check is meaningful for a rollback or
    a migration, not for a cache flush).
    """

    ROLLBACK = "rollback"
    RESTART = "restart"
    SCALE = "scale"
    CONFIG_CHANGE = "config_change"
    FAILOVER = "failover"
    MIGRATION = "migration"
    OTHER = "other"

    @property
    def changes_schema_surface(self) -> bool:
        return self in _SCHEMA_SURFACE_ACTIONS


_SCHEMA_SURFACE_ACTIONS = frozenset({ActionKind.ROLLBACK, ActionKind.MIGRATION})


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"


class ProposedActionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    HELD = "held"

    @property
    def is_terminal(self) -> bool:
        return self is not ProposedActionStatus.PENDING


class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return _RISK_TIER_RANK[self]

    @classmethod
    def max(cls, *tiers: "RiskTier") -> "RiskTier":
        """Escalation is monotonic: the verdict is the highest tier any
        individual check produced."""
        return max(tiers, key=lambda tier: tier.rank, default=cls.LOW)


_RISK_TIER_RANK = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


class GovernorAction(str, Enum):
    """Intervention Governor state machine output (SSOT §5 item 5)."""

    SILENT = "SILENT"
    SUGGEST = "SUGGEST"
    ASK = "ASK"
    WARN = "WARN"

    @property
    def is_spoken(self) -> bool:
        """Does this action consume the rate-limit window?"""
        return self in _SPOKEN_GOVERNOR_ACTIONS


_SPOKEN_GOVERNOR_ACTIONS = frozenset(
    {GovernorAction.SUGGEST, GovernorAction.ASK, GovernorAction.WARN}
)


class EvidenceSourceType(str, Enum):
    TELEMETRY = "telemetry"
    VISUAL = "visual"


class EvidenceSource(str, Enum):
    MOCK_TELEMETRY = "mock_telemetry"
    SCREENSHOT_UPLOAD = "screenshot_upload"


class ExtractionCertainty(str, Enum):
    """Categorical by design -- never a probability score.

    SSOT §26 non-goal / §25 decision #14: no Bayesian or probabilistic
    confidence modelling, for spoken claims or visual evidence. This flag
    feeds a hard-coded deterministic branch, nothing more.
    """

    HIGH = "high"
    LOW = "low"


class TopologyEdgeType(str, Enum):
    DEPENDS_ON = "depends_on"
    READS_SCHEMA = "reads_schema"
    COMPATIBLE_WITH = "compatible_with"


class RiskFindingCode(str, Enum):
    """Machine-readable identity of each risk finding.

    ``RiskVerdict.reasons`` carries the human-readable sentences AEGIS
    speaks aloud; these codes carry the same information in a form the
    evaluation harness, the UI and the structured logs can assert on
    without string-matching prose.
    """

    STALE_JUSTIFICATION = "stale_justification"
    DECISION_REVERSAL = "decision_reversal"
    BLAST_RADIUS_SCHEMA_BREAK = "blast_radius_schema_break"
    EVIDENCE_CONTRADICTION = "evidence_contradiction"
    EVIDENCE_CONTRADICTION_LOW_CERTAINTY = "evidence_contradiction_low_certainty"

    #: A human said something that reads as a decision, but which action it
    #: answers cannot be established. This is a finding rather than a special
    #: case because it is a risk in exactly the same sense as the others: the
    #: cheap resolution is to guess, and guessing here attaches a person's
    #: "yes" to an action they were not talking about.
    AMBIGUOUS_CONFIRMATION = "ambiguous_confirmation"


class InterventionOutcome(str, Enum):
    """Why a governor decision did or did not reach the humans -- recorded
    for observability (Quality Standard §14: "Why did AEGIS intervene (or
    not)?")."""

    SPOKEN = "spoken"
    SUPPRESSED_LOW_RISK = "suppressed_low_risk"
    SUPPRESSED_ALREADY_SAID = "suppressed_already_said"
    QUEUED_RATE_LIMITED = "queued_rate_limited"
    DROPPED_STALE_ON_REPLAY = "dropped_stale_on_replay"
    DELIVERY_FAILED = "delivery_failed"

    #: Decided, but not worth saying late. Some interventions only make sense
    #: in the moment -- asking "which one did you mean?" forty-five seconds
    #: after the question was asked is worse than staying quiet.
    SUPPRESSED_NOT_WORTH_SAYING_LATE = "suppressed_not_worth_saying_late"
