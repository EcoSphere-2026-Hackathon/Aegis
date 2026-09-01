"""
Mocked telemetry: four fixed metrics, deliberately.

SSOT §25 decision #6 fixes the scope at exactly four mocked metrics. That is
a reliability decision, not a shortcut, and it is disclosed rather than
hidden -- a real monitoring integration inside a three-day window would be a
demo that fails in front of judges for reasons unrelated to the idea.

What matters architecturally is that this is an *evidence producer*, not a
pipeline. It emits :class:`Evidence` objects that flow into the same
``risk_engine.evaluate()`` call as screenshot-sourced evidence, exactly as
SSOT §29 generalises. Adding a second producer later changes nothing
downstream.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.enums import EvidenceSource, EvidenceSourceType, ExtractionCertainty
from backend.common.errors import AegisError
from backend.common.models import Evidence


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    value: float | str
    unit: Optional[str]
    target_ref: Optional[str]
    description: str
    aliases: tuple[str, ...] = ()
    """How people actually say this metric out loud.

    Listed explicitly rather than derived from the name, because deriving
    them produces false matches: the last token of ``schema_version`` is
    "version", which would make "roll Core back to the last version" look
    like a claim about a metric."""


#: The four metrics, and the reason each one is in the set.
TELEMETRY_DEFINITIONS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        name="pool_utilization",
        value=91.0,
        unit="%",
        target_ref="core-db",
        description=(
            "Connection pool saturation. Set to 91 because the golden demo turns on "
            "someone reporting it as 'fine, like 40%' -- the gap between the claim and "
            "the reading is the first killer moment."
        ),
        aliases=("pool", "the pool", "connection pool", "pool utilisation", "pool saturation"),
    ),
    MetricDefinition(
        name="error_rate",
        value=12.4,
        unit="%",
        target_ref="payment-api",
        description="Request error rate, consistent with the incident being real.",
        aliases=("error rate", "errors", "error percentage"),
    ),
    MetricDefinition(
        name="p99_latency",
        value=2400.0,
        unit="ms",
        target_ref="payment-api",
        description="Tail latency, high enough to corroborate the reported timeouts.",
        aliases=("p99", "latency", "tail latency", "p99 latency"),
    ),
    MetricDefinition(
        name="schema_version",
        value="v17",
        unit=None,
        target_ref="core-db",
        description=(
            "Deployed schema version. Non-numeric on purpose: it exercises the "
            "engine's handling of readings that cannot be compared numerically."
        ),
        aliases=("schema", "schema version"),
    ),
)

TELEMETRY_METRICS: tuple[str, ...] = tuple(d.name for d in TELEMETRY_DEFINITIONS)

#: Metric name -> the phrases people use for it. Consumed by the extraction
#: prompt (so the model maps "the pool" to a real metric) and by the offline
#: extractor (so it can do the same without a model).
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    definition.name: definition.aliases for definition in TELEMETRY_DEFINITIONS
}

_DEFINITIONS_BY_NAME: Mapping[str, MetricDefinition] = {d.name: d for d in TELEMETRY_DEFINITIONS}


class UnknownMetricError(AegisError):
    code = "unknown_metric"


class MockTelemetry:
    """A read-only metrics source.

    Read-only is a safety property, not a convenience: SSOT §17 restricts
    tools to bounded, reversible actions, and a telemetry query that could
    mutate anything would break that. There is no write path here at all.

    Values are overridable at runtime so a rehearsal can walk a metric
    through a scenario (91% -> 38% after a fix) without editing code. The
    override is in-memory and per-instance; nothing persists.
    """

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._overrides: dict[str, float | str] = {}

    @property
    def metric_names(self) -> tuple[str, ...]:
        return TELEMETRY_METRICS

    @property
    def metric_aliases(self) -> dict[str, tuple[str, ...]]:
        return dict(METRIC_ALIASES)

    def describe(self) -> tuple[dict, ...]:
        return tuple(
            {
                "name": definition.name,
                "unit": definition.unit,
                "target_ref": definition.target_ref,
                "description": definition.description,
                "current_value": self._current_value(definition),
            }
            for definition in TELEMETRY_DEFINITIONS
        )

    def set_value(self, metric_name: str, value: float | str) -> None:
        if metric_name not in _DEFINITIONS_BY_NAME:
            raise UnknownMetricError("no such metric", metric_name=metric_name,
                                     known=list(TELEMETRY_METRICS))
        with self._lock:
            self._overrides[metric_name] = value

    def reset(self) -> None:
        with self._lock:
            self._overrides.clear()

    def read(self, metric_name: str) -> Evidence:
        """One reading, as an :class:`Evidence` object ready for the engine."""
        definition = _DEFINITIONS_BY_NAME.get(metric_name)
        if definition is None:
            raise UnknownMetricError("no such metric", metric_name=metric_name,
                                     known=list(TELEMETRY_METRICS))
        return Evidence(
            source_type=EvidenceSourceType.TELEMETRY,
            source=EvidenceSource.MOCK_TELEMETRY,
            metric_name=definition.name,
            value=self._current_value(definition),
            unit=definition.unit,
            extraction_certainty=ExtractionCertainty.HIGH,
            timestamp=self._clock.now(),
            target_ref=definition.target_ref,
        )

    def read_many(self, metric_names: Sequence[str]) -> tuple[Evidence, ...]:
        """Read several metrics, skipping names this source does not serve.

        Skipping rather than raising: a claim referencing an unknown metric is
        an extraction-quality problem, and it must not abort the grounding of
        the metrics that *are* available in the same turn.
        """
        readings: list[Evidence] = []
        for name in metric_names:
            if name in _DEFINITIONS_BY_NAME:
                readings.append(self.read(name))
        return tuple(readings)

    def read_all(self) -> tuple[Evidence, ...]:
        return tuple(self.read(name) for name in TELEMETRY_METRICS)

    def _current_value(self, definition: MetricDefinition) -> float | str:
        with self._lock:
            return self._overrides.get(definition.name, definition.value)
