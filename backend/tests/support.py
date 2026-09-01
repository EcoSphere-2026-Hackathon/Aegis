"""Shared test fixtures and builders.

Keeps the tests readable by giving every model a sensible default, so each
test states only the fields it is actually about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from backend.common.enums import (
    ActionKind,
    DecisionStance,
    EvidenceSource,
    EvidenceSourceType,
    ExtractionCertainty,
    HypothesisStatus,
    ProposedActionStatus,
    SourceModality,
)
from backend.common.models import (
    Decision,
    Evidence,
    Fact,
    Hypothesis,
    ProposedAction,
    StateSnapshot,
    TranscriptEvent,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """A timestamp ``seconds`` after the fixed test epoch."""
    return T0 + timedelta(seconds=seconds)


def make_fact(text: str = "payments are throwing 500s", *, uid: str = "1001", when: float = 0) -> Fact:
    return Fact(text=text, speaker_uid=uid, timestamp=at(when), source_turn_id="turn-fact")


def make_hypothesis(
    text: str = "might be the connection pool",
    *,
    uid: str = "1002",
    when: float = 0,
    status: HypothesisStatus = HypothesisStatus.ACTIVE,
    reinforcement_count: int = 0,
    target_ref: Optional[str] = None,
    metric_ref: Optional[str] = None,
    claimed_value: Optional[float] = None,
    claimed_unit: Optional[str] = None,
) -> Hypothesis:
    return Hypothesis(
        text=text,
        speaker_uid=uid,
        timestamp=at(when),
        status=status,
        reinforcement_count=reinforcement_count,
        target_ref=target_ref,
        metric_ref=metric_ref,
        claimed_value=claimed_value,
        claimed_unit=claimed_unit,
        source_turn_id="turn-hyp",
    )


def make_decision(
    text: str = "hold off on the rollback",
    *,
    uid: str = "1001",
    when: float = 0,
    target_ref: Optional[str] = "core-db",
    stance: Optional[DecisionStance] = DecisionStance.HOLD,
) -> Decision:
    return Decision(
        text=text,
        speaker_uid=uid,
        timestamp=at(when),
        target_ref=target_ref,
        stance=stance,
        source_turn_id="turn-dec",
    )


def make_action(
    text: str = "let's roll Core back to the last version",
    *,
    uid: str = "1001",
    when: float = 10,
    target_ref: str = "core-db",
    action_kind: ActionKind = ActionKind.ROLLBACK,
    target_schema_version: Optional[str] = "v2.3",
    justifying_hypothesis_id: Optional[str] = None,
    status: ProposedActionStatus = ProposedActionStatus.PENDING,
    resolved_by_uid: Optional[str] = None,
    resolved_at: Optional[float] = None,
) -> ProposedAction:
    return ProposedAction(
        text=text,
        target_ref=target_ref,
        speaker_uid=uid,
        timestamp=at(when),
        action_kind=action_kind,
        target_schema_version=target_schema_version,
        justifying_hypothesis_id=justifying_hypothesis_id,
        status=status,
        resolved_by_uid=resolved_by_uid,
        resolved_at=at(resolved_at) if resolved_at is not None else None,
        source_turn_id="turn-action",
    )


def make_telemetry(
    metric_name: str = "pool_utilization",
    value: float | str = 91,
    *,
    unit: Optional[str] = "%",
    when: float = 5,
    target_ref: Optional[str] = None,
) -> Evidence:
    return Evidence(
        source_type=EvidenceSourceType.TELEMETRY,
        source=EvidenceSource.MOCK_TELEMETRY,
        metric_name=metric_name,
        value=value,
        unit=unit,
        timestamp=at(when),
        target_ref=target_ref,
        extraction_certainty=ExtractionCertainty.HIGH,
    )


def make_visual_evidence(
    metric_name: str = "pool_utilization",
    value: float | str = 91,
    *,
    unit: Optional[str] = "%",
    when: float = 5,
    certainty: ExtractionCertainty = ExtractionCertainty.LOW,
    uploader_uid: str = "1002",
    target_ref: Optional[str] = None,
) -> Evidence:
    return Evidence(
        source_type=EvidenceSourceType.VISUAL,
        source=EvidenceSource.SCREENSHOT_UPLOAD,
        metric_name=metric_name,
        value=value,
        unit=unit,
        timestamp=at(when),
        extraction_certainty=certainty,
        uploader_uid=uploader_uid,
        target_ref=target_ref,
    )


def snapshot(
    *,
    facts: Sequence[Fact] = (),
    hypotheses: Sequence[Hypothesis] = (),
    decisions: Sequence[Decision] = (),
    proposed_actions: Sequence[ProposedAction] = (),
    when: float = 20,
) -> StateSnapshot:
    return StateSnapshot(
        captured_at=at(when),
        facts=tuple(facts),
        hypotheses=tuple(hypotheses),
        decisions=tuple(decisions),
        proposed_actions=tuple(proposed_actions),
    )


def transcript(
    text: str,
    *,
    uid: str = "1001",
    turn: str = "turn-1",
    when: float = 0,
    final: bool = True,
    modality: SourceModality = SourceModality.VOICE,
) -> TranscriptEvent:
    return TranscriptEvent(
        uid=uid,
        turn_id=turn,
        role="human",
        text=text,
        final=final,
        timestamp=at(when),
        source_modality=modality,
    )
