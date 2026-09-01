#!/usr/bin/env python3
"""
Extraction accuracy against a hand-labelled set.

Every other test in this project runs the deterministic provider, which is
the right call -- reasoning behaviour has to be reproducible, and a hosted
model makes it a coin flip. But it means none of them say anything about how
well a *real* model understands incident-room speech, and "our extraction is
good" is a claim nobody had measured.

This measures it. Labels in ``data/extraction_labels.json`` were authored
from what a careful incident responder would say each utterance means, before
any provider was run against them; deriving them from observed output would
make the exercise circular.

Two things are reported separately and must not be conflated:

* **The deterministic provider.** Always runnable, no credentials, and the
  number that gates the demo -- it is the provider the demo actually uses
  when no key is configured.
* **The configured LLM.** Runs only when ``LLM_PROVIDER`` and an API key are
  set. Without them this says so, plainly, and reports nothing for it. A
  fabricated number here would be worse than no number.

Metrics are the ones that matter for this system rather than the ones that
are easy: claim-type accuracy, the fact-versus-hypothesis split (confusing
those is how a guess drives an action), target and metric binding, and the
two false-positive classes that cost a spoken intervention -- inventing a
proposal, and reading an ambiguous reply as approval.

Run:  python scripts/eval_extraction.py [--provider deterministic|configured|both] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common.config import load_config  # noqa: E402
from backend.common.logging import configure_logging  # noqa: E402
from backend.common.models import TranscriptEvent  # noqa: E402
from backend.extraction.providers.deterministic import (
    DeterministicProvider,  # noqa: E402
)
from backend.extraction.service import ExtractionService  # noqa: E402
from backend.risk_engine.topology import build_incident_topology  # noqa: E402
from backend.telemetry.mock_telemetry import MockTelemetry  # noqa: E402
from backend.tests.support import at  # noqa: E402

configure_logging("CRITICAL")

LABELS_PATH = ROOT / "data" / "extraction_labels.json"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class CaseResult:
    case_id: str
    probes: str
    passed: bool
    got_types: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class Tally:
    """Counts kept per property rather than one blended score.

    A single accuracy number hides the distinction that matters here: missing
    a proposal and inventing one are both errors, and only one of them makes
    AEGIS interrupt a room for no reason.
    """

    checked: int = 0
    correct: int = 0

    def record(self, ok: bool) -> None:
        self.checked += 1
        self.correct += int(ok)

    @property
    def rate(self) -> Optional[float]:
        return None if not self.checked else self.correct / self.checked

    def render(self) -> str:
        if not self.checked:
            return f"{DIM}not exercised{RESET}"
        percent = 100 * (self.rate or 0)
        colour = GREEN if percent == 100 else (YELLOW if percent >= 80 else RED)
        return f"{colour}{self.correct}/{self.checked}  {percent:5.1f}%{RESET}"


def build_service(provider_name: str) -> tuple[Optional[ExtractionService], Optional[str]]:
    """Returns (service, reason it is unavailable)."""
    # Built exactly as the runtime builds it, aliases included. People say
    # "the pool", not "pool_utilization"; evaluating without the alias table
    # would measure a service the product never runs.
    telemetry = MockTelemetry()
    targets = build_incident_topology().nodes()
    metrics = telemetry.metric_names
    aliases = telemetry.metric_aliases

    if provider_name == "deterministic":
        return (
            ExtractionService(
                DeterministicProvider(),
                known_targets=targets,
                known_metrics=metrics,
                metric_aliases=aliases,
            ),
            None,
        )

    config = load_config()
    if config.llm.provider == "deterministic":
        return None, "LLM_PROVIDER is not configured (still the deterministic provider)"
    if not config.llm.api_key.reveal():
        return None, f"{config.llm.provider} is selected but no API key is configured"

    from backend.extraction.providers.openai_compatible import OpenAICompatibleProvider

    return (
        ExtractionService(
            OpenAICompatibleProvider(config.llm),
            known_targets=targets,
            known_metrics=metrics,
            metric_aliases=aliases,
        ),
        None,
    )


def run_case(service: ExtractionService, case: dict) -> tuple[CaseResult, dict[str, list[bool]]]:
    """Score one labelled utterance. Returns the result and per-property hits."""
    hits: dict[str, list[bool]] = {}

    def score(prop: str, ok: bool) -> bool:
        hits.setdefault(prop, []).append(ok)
        return ok

    event = TranscriptEvent(
        uid="1001",
        turn_id=f"eval-{case['id']}",
        role="human",
        text=case["text"],
        final=True,
        timestamp=at(10),
    )
    try:
        outcome = service.extract(
            event, pending_action_targets=tuple(case.get("pending", ()))
        )
    except Exception as exc:  # noqa: BLE001 - a provider blowing up is a result
        return CaseResult(case["id"], case.get("probes", ""), False, error=repr(exc)), hits

    claims = list(outcome.claims)
    got_types = sorted({claim.type.value for claim in claims})
    failures: list[str] = []

    expected_types = case.get("expect_types")
    if expected_types is not None:
        ok = score("claim_type", sorted(set(expected_types)) == got_types)
        if not ok:
            failures.append(f"types {got_types} != {sorted(set(expected_types))}")
        # The distinction that decides whether a guess can drive an action.
        if {"fact", "hypothesis"} & set(expected_types):
            wanted = "fact" if "fact" in expected_types else "hypothesis"
            score("fact_vs_hypothesis", wanted in got_types)

    if "expect_target" in case:
        targets = {claim.target_ref for claim in claims if claim.target_ref}
        ok = score("target_binding", case["expect_target"] in targets)
        if not ok:
            failures.append(f"target {sorted(targets) or 'none'} != {case['expect_target']}")

    if "expect_metric" in case:
        metrics = {claim.metric_ref for claim in claims if claim.metric_ref}
        ok = score("metric_binding", case["expect_metric"] in metrics)
        if not ok:
            failures.append(f"metric {sorted(metrics) or 'none'} != {case['expect_metric']}")

    if "expect_value" in case:
        values = {claim.claimed_value for claim in claims if claim.claimed_value is not None}
        ok = score("value_binding", case["expect_value"] in values)
        if not ok:
            failures.append(f"value {sorted(values) or 'none'} != {case['expect_value']}")

    if "expect_action_kind" in case:
        kinds = {claim.action_kind.value for claim in claims if claim.action_kind}
        ok = score("action_kind", case["expect_action_kind"] in kinds)
        if not ok:
            failures.append(f"kind {sorted(kinds) or 'none'} != {case['expect_action_kind']}")

    if "expect_schema_version" in case:
        versions = {
            claim.target_schema_version for claim in claims if claim.target_schema_version
        }
        ok = score("schema_version", case["expect_schema_version"] in versions)
        if not ok:
            failures.append(f"version {sorted(versions) or 'none'}")

    # False positives: each of these costs a spoken intervention or, worse,
    # an authorisation nobody gave.
    if case.get("must_not_propose"):
        ok = score("no_false_proposal", "proposed_action" not in got_types)
        if not ok:
            failures.append("invented a proposed action")

    if case.get("must_not_confirm"):
        ok = score("no_false_confirmation", "confirmation" not in got_types)
        if not ok:
            failures.append("read this as an authorisation")

    if case.get("must_not_override"):
        ok = score("no_false_refusal", "override" not in got_types)
        if not ok:
            failures.append("read this as a refusal")

    if case.get("must_not_invent_target"):
        invented = {claim.target_ref for claim in claims if claim.target_ref}
        ok = score("no_invented_target", not invented)
        if not ok:
            failures.append(f"invented target {sorted(invented)}")

    return (
        CaseResult(
            case_id=case["id"],
            probes=case.get("probes", ""),
            passed=not failures,
            got_types=got_types,
            failures=failures,
        ),
        hits,
    )


def evaluate(service: ExtractionService, cases: Sequence[dict]) -> dict:
    results: list[CaseResult] = []
    tallies: dict[str, Tally] = {}
    for case in cases:
        result, hits = run_case(service, case)
        results.append(result)
        for prop, outcomes in hits.items():
            tally = tallies.setdefault(prop, Tally())
            for ok in outcomes:
                tally.record(ok)
    return {"results": results, "tallies": tallies}


PROPERTY_ORDER = (
    ("claim_type", "Claim type"),
    ("fact_vs_hypothesis", "Fact vs hypothesis"),
    ("target_binding", "Component binding"),
    ("metric_binding", "Metric binding"),
    ("value_binding", "Value binding"),
    ("action_kind", "Action kind"),
    ("schema_version", "Schema version"),
    ("no_false_proposal", "No invented proposal"),
    ("no_false_confirmation", "No false approval"),
    ("no_false_refusal", "No false refusal"),
    ("no_invented_target", "No invented component"),
)


def render(label: str, report: dict, *, note: str = "") -> None:
    results: list[CaseResult] = report["results"]
    tallies: dict[str, Tally] = report["tallies"]
    passed = [r for r in results if r.passed]

    print(f"\n  {BOLD}{label}{RESET}")
    if note:
        print(f"  {DIM}{note}{RESET}")
    print(f"  {len(passed)}/{len(results)} utterances fully correct\n")

    for key, title in PROPERTY_ORDER:
        tally = tallies.get(key)
        if tally is None:
            continue
        print(f"    {title:<24} {tally.render()}")

    failures = [r for r in results if not r.passed]
    if failures:
        print(f"\n    {DIM}where it was wrong:{RESET}")
        for result in failures:
            detail = result.error or "; ".join(result.failures)
            print(f"      {RED}{result.case_id}{RESET}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS extraction evaluation")
    parser.add_argument(
        "--provider",
        choices=("deterministic", "configured", "both"),
        default="both",
        help="which extraction path to score",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    suite = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    cases = suite["cases"]

    payload: dict = {"suite": suite["name"], "cases": len(cases), "providers": {}}
    wanted = (
        ("deterministic", "configured")
        if args.provider == "both"
        else (args.provider,)
    )

    if not args.json:
        print(f"\n  {BOLD}AEGIS extraction evaluation{RESET}")
        print(f"  {DIM}{len(cases)} hand-labelled utterances, authored before any run{RESET}")

    exit_code = 0
    for name in wanted:
        service, unavailable = build_service(name)
        if service is None:
            payload["providers"][name] = {"available": False, "reason": unavailable}
            if not args.json:
                print(f"\n  {BOLD}{name.title()} provider{RESET}")
                print(f"  {YELLOW}not evaluated: {unavailable}{RESET}")
                print(
                    f"  {DIM}Configure LLM_PROVIDER and LLM_API_KEY and re-run to measure the"
                    f" real extraction path.{RESET}"
                )
            continue

        report = evaluate(service, cases)
        results: list[CaseResult] = report["results"]
        payload["providers"][name] = {
            "available": True,
            "cases_fully_correct": sum(1 for r in results if r.passed),
            "cases": len(results),
            "properties": {
                key: {
                    "checked": tally.checked,
                    "correct": tally.correct,
                    "rate": round(tally.rate, 4) if tally.rate is not None else None,
                }
                for key, tally in report["tallies"].items()
            },
            "failures": [
                {"id": r.case_id, "detail": r.error or "; ".join(r.failures)}
                for r in results
                if not r.passed
            ],
        }
        if not args.json:
            note = (
                "the provider the demo uses when no key is configured"
                if name == "deterministic"
                else "the real model path"
            )
            render(f"{name.title()} provider", report, note=note)
        if name == "deterministic" and any(not r.passed for r in results):
            exit_code = 1

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
