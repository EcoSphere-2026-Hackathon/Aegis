#!/usr/bin/env python3
"""
Canonical end-to-end reasoning check. One command, one verdict.

This is the harness that answers "is AEGIS actually working?" without a
person having to read output and decide. Every phase asserts, and the most
important assertions are the ones that fail when AEGIS does *nothing*: a
reasoning system that has gone silent looks identical to a calm one in a log,
and that is exactly the failure that survives a green unit-test suite.

It drives the real runtime -- real store, real extraction service, real risk
engine, real governor, real delivery -- through the same entry point the HTTP
and voice transports use. Nothing here reaches past the pipeline's front door,
so a pass means the product works, not that the parts do.

    python scripts/demo_e2e.py            # canonical incident, twice
    python scripts/demo_e2e.py --verbose  # show every claim and verdict

Exit code 0 only if every phase passes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, TypeVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import use_utf8_stdout  # noqa: E402

use_utf8_stdout()

from backend.common.clock import ManualClock  # noqa: E402
from backend.common.config import (  # noqa: E402
    AppConfig,
    GovernorConfig,
    load_config,
)
from backend.common.enums import (  # noqa: E402
    ClaimType,
    ProposedActionStatus,
    RiskFindingCode,
    RiskTier,
    SourceModality,
)
from backend.common.models import TranscriptEvent  # noqa: E402
from backend.pipeline.factory import build_runtime  # noqa: E402
from backend.pipeline.sinks import RecordingSink  # noqa: E402
from backend.tests.support import T0  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

VERBOSE = False


class Failure(AssertionError):
    """A product-level expectation that did not hold."""


class Incident:
    """One incident, driven the way a transport drives it."""

    def __init__(self) -> None:
        self.clock = ManualClock(start=T0)
        base = load_config(env={}, dotenv_path=None, project_root=None)
        config = AppConfig(
            agora=base.agora,
            llm=base.llm,
            governor=GovernorConfig(rate_limit_seconds=45.0),
            pipeline=base.pipeline,
            api=base.api,
            database_path=base.database_path,
            incident_id="demo-e2e",
            log_level="CRITICAL",
        )
        self.sink = RecordingSink(clock=self.clock)
        self.rt = build_runtime(
            config, clock=self.clock, sink=self.sink, database_path=":memory:"
        )
        self._turn = 0

    # -- driving ---------------------------------------------------------

    def say(self, text: str, *, uid: str = "1001", advance: float = 6.0):
        """One operator utterance, through the canonical ingestion path."""
        self._turn += 1
        self.clock.advance(advance)
        result = self.rt.pipeline.handle_transcript(
            TranscriptEvent(
                uid=uid,
                turn_id=f"e2e-{self._turn}",
                role="human",
                text=text,
                final=True,
                timestamp=self.clock.now(),
                source_modality=SourceModality.VOICE,
            )
        )
        if VERBOSE:
            print(f"    {DIM}> {text}{RESET}")
            for claim in result.claims:
                print(
                    f"      {DIM}claim {claim.type.value}"
                    f" target={claim.target_ref} metric={claim.metric_ref}"
                    f" value={claim.claimed_value}{RESET}"
                )
            for line in result.spoken:
                print(f"      {YELLOW}AEGIS: {line}{RESET}")
        return result

    def metric(self, name: str, value) -> None:
        self.rt.telemetry.set_value(name, value)

    # -- observing -------------------------------------------------------

    @property
    def spoken(self) -> tuple:
        return self.sink.lines

    def actions(self):
        return self.rt.store.snapshot(captured_at=self.clock.now()).proposed_actions

    def action_on(self, target: str):
        for action in self.actions():
            if action.target_ref == target:
                return action
        return None

    def hypotheses(self):
        return self.rt.store.snapshot(captured_at=self.clock.now()).hypotheses

    def open_window(self) -> None:
        """Move past the rate limit so the next finding is not merely queued."""
        self.clock.advance(60)

    def close(self) -> None:
        self.rt.close()


# -- assertions ----------------------------------------------------------


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"    {GREEN}PASS{RESET}  {label}")
        return
    print(f"    {RED}FAIL{RESET}  {label}")
    if detail:
        print(f"          {DIM}{detail}{RESET}")
    raise Failure(label)


T = TypeVar("T")


def require(value: Optional[T], what: str) -> T:
    """Fail the run rather than crash on a missing intermediate.

    A harness whose job is to prove the pipeline produced something should
    report "no verdict was attached" and stop, not raise AttributeError three
    frames later on a None.
    """
    if value is None:
        print(f"    {RED}FAIL{RESET}  {what}")
        raise Failure(what)
    return value


# -- phases --------------------------------------------------------------


def phase_understanding(incident: Incident) -> None:
    """Speech becomes typed claims with the right shapes."""
    print(f"\n  {BOLD}1. Understanding{RESET}  {DIM}speech -> typed claims{RESET}")

    incident.metric("error_rate", 12.0)
    incident.metric("pool_utilization", 92.0)
    result = incident.say(
        "The api-gateway error rate has increased to 12 percent."
    )
    check(
        "a stated measurement becomes a grounded claim",
        any(c.metric_ref == "error_rate" for c in result.claims),
        f"claims={[(c.type.value, c.metric_ref) for c in result.claims]}",
    )

    result = incident.say(
        "The retry storm appears to be causing the increase in errors."
    )
    check(
        "a hedged causal statement is a hypothesis, not a fact",
        any(c.type is ClaimType.HYPOTHESIS for c in result.claims),
        f"claims={[c.type.value for c in result.claims]}",
    )


def phase_risky_action(incident: Incident) -> None:
    """A destructive proposal is evaluated and actually reaches the governor."""
    print(f"\n  {BOLD}2. Risk{RESET}  {DIM}proposal -> verdict -> intervention{RESET}")

    incident.open_window()
    before = len(incident.spoken)
    incident.say("Let's roll back core-db to version v2.3.")

    found = incident.action_on("core-db")
    check(
        "a destructive proposal becomes a tracked action",
        found is not None,
        f"actions={[(a.action_kind.value, a.target_ref) for a in incident.actions()]}",
    )
    action = require(found, "action on core-db")
    verdict = require(action.risk_verdict, "the risk engine ran on the action")
    check("the risk engine actually ran on it", True)
    check(
        "a schema-breaking rollback is not rated LOW",
        verdict.risk_tier is not RiskTier.LOW,
        f"tier={verdict.risk_tier.value}",
    )
    # The point of the whole product: it says something.
    check(
        "AEGIS spoke about it",
        len(incident.spoken) > before,
        "the governor produced no audible intervention for a HIGH-risk rollback",
    )
    check(
        "and it stayed pending -- nothing was authorised",
        action.status is ProposedActionStatus.PENDING,
        f"status={action.status.value}",
    )


def phase_ambiguity(incident: Incident) -> None:
    """Two open actions plus a bare yes must authorise nothing."""
    print(f"\n  {BOLD}3. Ambiguity{RESET}  {DIM}refusal, not a guess{RESET}")

    incident.open_window()
    incident.say("Let's also restart notification-service.")
    check(
        "a second action is open",
        incident.action_on("notification-service") is not None,
        f"actions={[a.target_ref for a in incident.actions()]}",
    )

    incident.say("Yeah, go ahead.")
    still_pending = [
        a for a in incident.actions() if a.status is ProposedActionStatus.PENDING
    ]
    check(
        "an ambiguous yes authorises nothing",
        len(still_pending) == 2,
        f"statuses={[(a.target_ref, a.status.value) for a in incident.actions()]}",
    )


def phase_explicit_confirmation(incident: Incident) -> None:
    """A named target resolves exactly one action, and only that one."""
    print(f"\n  {BOLD}4. Confirmation{RESET}  {DIM}named target resolves one{RESET}")

    incident.say("Yes, roll back core-db.")
    core = require(incident.action_on("core-db"), "core-db action present")
    other = require(
        incident.action_on("notification-service"), "notification-service action present"
    )
    check(
        "the named action is confirmed",
        core.status is ProposedActionStatus.CONFIRMED,
        f"status={core.status.value}",
    )
    check(
        "it is attributed to the human who said it",
        core.resolved_by_uid == "1001",
        f"resolved_by={core.resolved_by_uid}",
    )
    check(
        "the action nobody named is untouched",
        other.status is ProposedActionStatus.PENDING,
        f"status={other.status.value}",
    )


def phase_retraction() -> None:
    """Contradicting a belief re-opens what was concluded from it."""
    print(f"\n  {BOLD}5. Belief retraction{RESET}  {DIM}reality moves, conclusions follow{RESET}")

    incident = Incident()
    try:
        # A theory telemetry agrees with, and a low-risk action resting on it.
        incident.metric("error_rate", 12.0)
        incident.say("Error rate is around 12 percent, the retry storm is the cause.")
        result = incident.say("Let's roll back search-index then.")
        action_id = next(
            (c.claim_id for c in result.claims if c.type is ClaimType.PROPOSED_ACTION),
            None,
        )
        aid = require(action_id, "a proposed action was produced")
        stored = require(
            incident.rt.store.get_proposed_action(aid), "the action was stored"
        )
        check(
            "the action records the theory that justified it",
            stored.justifying_hypothesis_id is not None,
            "no justification recorded -- retraction has no edge to walk",
        )
        first = stored.risk_verdict

        # Reality moves.
        incident.open_window()
        incident.metric("error_rate", 0.3)
        incident.say("Error rate is down to 0.3 percent now.")

        after = require(
            incident.rt.store.get_proposed_action(aid), "the action survived re-evaluation"
        )
        after_verdict = require(after.risk_verdict, "a fresh verdict was attached")
        check(
            "the dependent action was re-evaluated upward",
            first is None or after_verdict.risk_tier.rank > first.risk_tier.rank,
            f"before={first.risk_tier.value if first else None} "
            f"after={after_verdict.risk_tier.value}",
        )
        check(
            "the reason names the collapsed justification",
            RiskFindingCode.STALE_JUSTIFICATION in after_verdict.codes,
            f"codes={[c.value for c in after_verdict.codes]}",
        )
        check(
            "AEGIS did not silently alter the action",
            after.status is ProposedActionStatus.PENDING,
            f"status={after.status.value}",
        )
    finally:
        incident.close()


def phase_evidence_path() -> None:
    """The same retraction, arriving as evidence rather than speech."""
    print(f"\n  {BOLD}6. Multimodal retraction{RESET}  {DIM}/api/evidence door{RESET}")

    from backend.common.enums import (
        EvidenceSource,
        EvidenceSourceType,
        ExtractionCertainty,
    )
    from backend.common.models import Evidence

    incident = Incident()
    try:
        incident.metric("error_rate", 12.0)
        incident.say("Error rate is around 12 percent, the retry storm is the cause.")
        result = incident.say("Let's roll back search-index then.")
        action_id = next(
            c.claim_id for c in result.claims if c.type is ClaimType.PROPOSED_ACTION
        )
        before = require(
            incident.rt.store.get_proposed_action(action_id), "action stored"
        ).risk_verdict

        incident.open_window()
        incident.metric("error_rate", 0.3)
        incident.rt.pipeline.ingest_evidence(
            Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name="error_rate",
                value=0.3,
                unit="%",
                extraction_certainty=ExtractionCertainty.HIGH,
                uploader_uid="1001",
                timestamp=incident.clock.now(),
            )
        )
        after = require(
            incident.rt.store.get_proposed_action(action_id), "action survived"
        )
        after_verdict = require(after.risk_verdict, "a verdict was attached")
        check(
            "evidence retracts a belief the same way speech does",
            RiskFindingCode.STALE_JUSTIFICATION in after_verdict.codes,
            f"before={before.risk_tier.value if before else None} "
            f"codes={[c.value for c in after_verdict.codes]}",
        )
    finally:
        incident.close()


def phase_idempotency() -> None:
    """A redelivered turn changes nothing."""
    print(f"\n  {BOLD}7. Idempotency{RESET}  {DIM}redelivery is not a second event{RESET}")

    incident = Incident()
    try:
        event = TranscriptEvent(
            uid="1001",
            turn_id="dup-1",
            role="human",
            text="Let's roll back core-db to version v2.3.",
            final=True,
            timestamp=incident.clock.now(),
            source_modality=SourceModality.VOICE,
        )
        incident.rt.pipeline.handle_transcript(event)
        count = len(incident.actions())
        again = incident.rt.pipeline.handle_transcript(event)
        check(
            "the duplicate is reported as one",
            again.duplicate,
            "the pipeline treated a redelivered turn as new",
        )
        check(
            "and produced no second action",
            len(incident.actions()) == count,
            f"actions before={count} after={len(incident.actions())}",
        )
    finally:
        incident.close()


def phase_silence() -> None:
    """It stays quiet when nothing is wrong. Crying wolf is a failure too."""
    print(f"\n  {BOLD}8. Silence{RESET}  {DIM}ordinary chatter says nothing{RESET}")

    incident = Incident()
    try:
        for line in (
            "Morning everyone, joining now.",
            "Can you hear me okay?",
            "Let's give it a minute for the others.",
        ):
            incident.say(line)
        check(
            "no intervention on ordinary conversation",
            len(incident.spoken) == 0,
            f"spoke {len(incident.spoken)}x: {incident.spoken}",
        )
        check(
            "and no actions were invented",
            len(incident.actions()) == 0,
            f"actions={[a.target_ref for a in incident.actions()]}",
        )
    finally:
        incident.close()


def phase_self_echo() -> None:
    """AEGIS hearing itself must not become an operator decision."""
    print(f"\n  {BOLD}9. Self-echo{RESET}  {DIM}it must not hear itself into a decision{RESET}")

    from backend.common.config import PipelineConfig

    incident = Incident.__new__(Incident)
    incident.clock = ManualClock(start=T0)
    base = load_config(env={}, dotenv_path=None, project_root=None)
    incident.sink = RecordingSink(clock=incident.clock)
    incident.rt = build_runtime(
        AppConfig(
            agora=base.agora,
            llm=base.llm,
            governor=GovernorConfig(rate_limit_seconds=45.0),
            pipeline=PipelineConfig(agent_uid="9000"),
            api=base.api,
            database_path=base.database_path,
            incident_id="demo-echo",
            log_level="CRITICAL",
        ),
        clock=incident.clock,
        sink=incident.sink,
        database_path=":memory:",
    )
    incident._turn = 0
    try:
        incident.say("Let's roll back core-db to version v2.3.")
        opened = len(incident.actions())
        check("the operator's proposal is open", opened == 1, f"actions={opened}")

        # Exactly what returns when AEGIS's own audio is transcribed.
        incident.say(
            "Hold - rolling back core-db will break auth-service. "
            "Do you want to go ahead anyway?",
            uid="9000",
        )
        incident.say("Yes, go ahead with core-db.", uid="9000")

        core = require(incident.action_on("core-db"), "core-db action still present")
        check(
            "AEGIS did not authorise its own proposal",
            core.status is ProposedActionStatus.PENDING,
            f"status={core.status.value} by={core.resolved_by_uid}",
        )
        check(
            "and its own warning did not mint a new action",
            len(incident.actions()) == opened,
            f"actions={[a.target_ref for a in incident.actions()]}",
        )

        incident.say("Yes, go ahead with core-db.", uid="1001")
        core = require(incident.action_on("core-db"), "core-db action still present")
        check(
            "the operator is still heard",
            core.status is ProposedActionStatus.CONFIRMED and core.resolved_by_uid == "1001",
            f"status={core.status.value} by={core.resolved_by_uid}",
        )
    finally:
        incident.close()


def phase_unassessable_target() -> None:
    """An action the graph cannot locate is reported, not passed over."""
    print(f"\n  {BOLD}10. Unassessable target{RESET}  {DIM}unlocatable is not safe{RESET}")

    from backend.common.enums import ActionKind
    from backend.common.models import ProposedAction
    from backend.risk_engine.engine import evaluate
    from backend.risk_engine.topology import build_incident_topology
    from backend.state_store.store import IncidentStateStore

    with IncidentStateStore(":memory:", incident_id="probe") as store:
        snapshot = store.snapshot(captured_at=T0)
    action = ProposedAction(
        text="restart the memcached session store",
        target_ref="memcached-session-store",
        speaker_uid="1001",
        timestamp=T0,
        action_kind=ActionKind.RESTART,
        source_turn_id="probe",
    )
    verdict = evaluate(action, snapshot, build_incident_topology())
    check(
        "a destructive action on an unknown component is not rated LOW",
        verdict.risk_tier is not RiskTier.LOW,
        f"tier={verdict.risk_tier.value} codes={[c.value for c in verdict.codes]}",
    )
    check(
        "and the reason says it could not be assessed",
        RiskFindingCode.UNASSESSABLE_TARGET in verdict.codes,
        f"codes={[c.value for c in verdict.codes]}",
    )


def run_canonical() -> None:
    incident = Incident()
    try:
        phase_understanding(incident)
        phase_risky_action(incident)
        phase_ambiguity(incident)
        phase_explicit_confirmation(incident)
    finally:
        incident.close()
    phase_retraction()
    phase_evidence_path()
    phase_idempotency()
    phase_silence()
    phase_self_echo()
    phase_unassessable_target()


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="AEGIS end-to-end reasoning check")
    parser.add_argument("--verbose", action="store_true", help="show claims and verdicts")
    parser.add_argument(
        "--once", action="store_true", help="skip the repeat run from a clean reset"
    )
    args = parser.parse_args()
    VERBOSE = args.verbose

    runs = 1 if args.once else 2
    for run in range(1, runs + 1):
        label = "canonical incident" if run == 1 else "repeat from a clean start"
        print(f"\n{BOLD}  AEGIS end-to-end -- {label}{RESET}")
        try:
            run_canonical()
        except Failure as failure:
            print(f"\n  {RED}{BOLD}FAILED{RESET}  {failure}")
            print(f"  {DIM}The reasoning path is broken. Nothing below this line ran.{RESET}\n")
            return 1

    print(f"\n  {GREEN}{BOLD}All phases passed.{RESET} "
          f"{DIM}Understanding, risk, ambiguity, confirmation, retraction, "
          f"idempotency and silence.{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
