"""
Assembly.

One place where the object graph is wired, so tests, the replay harness and
the live server all run the *same* pipeline rather than three subtly
different ones. Anything that differs between them (which provider, which
sink, which clock) is a parameter, not a separate construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.config import AppConfig, load_config
from backend.common.logging import get_logger
from backend.common.metrics import Metrics
from backend.extraction.contracts import ExtractionProvider
from backend.extraction.providers.deterministic import DeterministicProvider
from backend.extraction.service import ExtractionService
from backend.governor.governor import Governor
from backend.pipeline.events import EventBus
from backend.pipeline.orchestrator import IncidentPipeline
from backend.pipeline.sinks import InterventionSink, RecordingSink
from backend.risk_engine.topology import Topology, build_incident_topology
from backend.state_store.store import IncidentStateStore
from backend.telemetry.mock_telemetry import MockTelemetry

_log = get_logger("factory")


@dataclass
class AegisRuntime:
    """Everything a running AEGIS instance is made of."""

    config: AppConfig
    store: IncidentStateStore
    topology: Topology
    telemetry: MockTelemetry
    governor: Governor
    extraction: ExtractionService
    pipeline: IncidentPipeline
    events: EventBus
    sink: InterventionSink
    metrics: Metrics

    def reset(self) -> None:
        """Start a fresh incident without restarting the process.

        The demo has to be runnable twice, and every piece of per-incident
        state has to go -- not just the database. A reset that cleared the
        store but left the governor's closed window and already-said set
        would produce a second run in which AEGIS says nothing at all, which
        looks exactly like a broken product and is the hardest failure to
        diagnose while someone is watching.

        Deliberately not a general "reload": configuration, the topology and
        the wiring stay exactly as they are, so what a rehearsal proved about
        the process remains true after this.
        """
        self.store.reset_incident()
        self.governor.reset()
        self.extraction.reset()
        self.telemetry.reset()
        self.metrics.reset()
        _log.info("incident reset", incident_id=self.config.incident_id)

    def close(self) -> None:
        self.store.close()


def build_provider(config: AppConfig) -> ExtractionProvider:
    """Select the extraction provider, falling back rather than failing.

    A missing key is a degraded mode, not a dead process: the reasoning
    layer still works, the demo still runs, and the structured logs record
    which provider produced every claim so nobody can mistake a fallback run
    for a live-model one.
    """
    if config.llm.provider in {"deterministic", "offline", "rules"}:
        return DeterministicProvider()

    if not config.llm.api_key:
        _log.warning(
            "no LLM API key configured; falling back to the deterministic extractor",
            configured_provider=config.llm.provider,
        )
        return DeterministicProvider()

    from backend.extraction.providers.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(config.llm)


def build_runtime(
    config: Optional[AppConfig] = None,
    *,
    clock: Clock = SYSTEM_CLOCK,
    provider: Optional[ExtractionProvider] = None,
    sink: Optional[InterventionSink] = None,
    database_path: Optional[str] = None,
) -> AegisRuntime:
    config = config or load_config()

    metrics = Metrics()
    topology = build_incident_topology()
    telemetry = MockTelemetry(clock=clock)
    store = IncidentStateStore(
        database_path if database_path is not None else config.database_path,
        incident_id=config.incident_id,
    )
    events = EventBus(clock=clock)
    governor = Governor(config.governor, clock=clock)

    extraction = ExtractionService(
        provider or build_provider(config),
        clock=clock,
        max_attempts=config.llm.max_attempts,
        known_targets=topology.nodes(),
        known_metrics=telemetry.metric_names,
        metric_aliases=telemetry.metric_aliases,
        metrics=metrics,
    )

    resolved_sink = sink or RecordingSink(clock=clock)
    pipeline = IncidentPipeline(
        store=store,
        extraction=extraction,
        governor=governor,
        topology=topology,
        telemetry=telemetry,
        sink=resolved_sink,
        events=events,
        clock=clock,
        config=config.pipeline,
        metrics=metrics,
    )

    return AegisRuntime(
        config=config,
        store=store,
        topology=topology,
        telemetry=telemetry,
        governor=governor,
        extraction=extraction,
        pipeline=pipeline,
        events=events,
        sink=resolved_sink,
        metrics=metrics,
    )
