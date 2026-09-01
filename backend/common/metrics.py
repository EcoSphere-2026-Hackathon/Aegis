"""
In-process instrumentation.

You cannot argue that an optimisation worked without a number, and you cannot
produce a number after the fact. So the counters and the latency reservoirs
live on the hot path from the start, and `/api/metrics` reads them.

Deliberately not Prometheus, not statsd, not OpenTelemetry. There is one
process, one incident and a judge with four minutes. A dependency-free
histogram that answers "which stage is slow, and how often did we avoid the
LLM" is worth more here than a metrics stack nobody will scrape.

Percentiles come from a bounded reservoir rather than an exact sort over
unbounded history: constant memory, and at the sample counts a demo produces
the error is irrelevant. The reservoir keeps the most recent N samples,
which is the right bias — the last minute of an incident matters more than
its first.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Iterator

#: Samples retained per stage. 512 doubles covers a long rehearsal at a few
#: kilobytes and keeps p95 stable.
RESERVOIR_SIZE = 512


class LatencyReservoir:
    """Recent-biased latency samples for one stage."""

    __slots__ = ("_samples", "_lock", "_count", "_total_ms", "_max_ms")

    def __init__(self, size: int = RESERVOIR_SIZE) -> None:
        self._samples: deque[float] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._count = 0
        self._total_ms = 0.0
        self._max_ms = 0.0

    def record(self, milliseconds: float) -> None:
        with self._lock:
            self._samples.append(milliseconds)
            self._count += 1
            self._total_ms += milliseconds
            if milliseconds > self._max_ms:
                self._max_ms = milliseconds

    def snapshot(self) -> dict:
        with self._lock:
            samples = sorted(self._samples)
            count = self._count
            total = self._total_ms
            peak = self._max_ms

        if not samples:
            return {"count": 0}

        return {
            "count": count,
            "p50_ms": round(_percentile(samples, 0.50), 2),
            "p95_ms": round(_percentile(samples, 0.95), 2),
            "max_ms": round(peak, 2),
            "mean_ms": round(total / count, 2) if count else 0.0,
        }


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated: with a few dozen samples an
    interpolated p95 invents a value that never occurred, and the honest
    answer to "what was the slow case" is a measurement that actually
    happened.
    """
    if not sorted_samples:
        return 0.0
    index = max(0, min(len(sorted_samples) - 1, int(round(fraction * len(sorted_samples) + 0.5)) - 1))
    return sorted_samples[index]


class Counter:
    __slots__ = ("_value", "_lock")

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class Metrics:
    """One registry per runtime. Cheap enough to call from anywhere."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, LatencyReservoir] = {}
        self._counters: dict[str, Counter] = {}

    def stage(self, name: str) -> LatencyReservoir:
        with self._lock:
            reservoir = self._stages.get(name)
            if reservoir is None:
                reservoir = LatencyReservoir()
                self._stages[name] = reservoir
            return reservoir

    def counter(self, name: str) -> Counter:
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = Counter()
                self._counters[name] = counter
            return counter

    def increment(self, name: str, amount: int = 1) -> None:
        self.counter(name).increment(amount)

    @contextmanager
    def time(self, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stage(stage).record((time.perf_counter() - started) * 1000.0)

    def record(self, stage: str, milliseconds: float) -> None:
        self.stage(stage).record(milliseconds)

    def reset(self) -> None:
        """Start the numbers over, so a fresh incident reports its own.

        Carrying the previous run's latencies and counters into the next one
        would make ``/api/metrics`` describe a conversation that is no longer
        on screen -- worse than showing nothing, because it looks
        authoritative.
        """
        with self._lock:
            self._stages.clear()
            self._counters.clear()

    def snapshot(self) -> dict:
        with self._lock:
            stages = dict(self._stages)
            counters = dict(self._counters)
        return {
            "stages": {name: reservoir.snapshot() for name, reservoir in sorted(stages.items())},
            "counters": {name: counter.value for name, counter in sorted(counters.items())},
        }

    def derived(self) -> dict:
        """Ratios worth stating outright rather than making a reader divide.

        A hit rate is the number that answers "did the cache earn its
        complexity"; leaving the reader to compute it from two counters is
        how instrumentation goes unread.
        """
        counters = {name: counter.value for name, counter in self._counters.items()}
        served = counters.get(EXTRACTION_REQUESTS, 0)
        cached = counters.get(EXTRACTION_CACHE_HITS, 0)
        fast_path = counters.get(EXTRACTION_FAST_PATH, 0)
        provider_calls = counters.get(EXTRACTION_PROVIDER_CALLS, 0)

        avoided = cached + fast_path
        return {
            "extraction_requests": served,
            "provider_calls": provider_calls,
            "provider_calls_avoided": avoided,
            "avoidance_rate": round(avoided / served, 4) if served else 0.0,
            "cache_hit_rate": round(cached / served, 4) if served else 0.0,
        }


# Stage names, fixed so the API and any later analysis agree on one vocabulary.
STAGE_EXTRACTION = "extraction"
STAGE_RISK_EVAL = "risk_evaluation"
STAGE_STATE_WRITE = "state_write"
STAGE_TURN_TOTAL = "turn_total"
STAGE_GOVERNOR = "governor_decision"
STAGE_SPEAK = "speak_delivery"
STAGE_WORKING_SET = "working_set_query"

# Counter names.
EXTRACTION_REQUESTS = "extraction_requests"
EXTRACTION_CACHE_HITS = "extraction_cache_hits"
EXTRACTION_FAST_PATH = "extraction_fast_path_hits"
EXTRACTION_PROVIDER_CALLS = "extraction_provider_calls"
EXTRACTION_DEGRADED = "extraction_degraded"
INTERVENTIONS_SPOKEN = "interventions_spoken"
INTERVENTIONS_QUEUED = "interventions_queued"
INTERVENTIONS_PREEMPTED = "interventions_preempted"
INTERVENTIONS_EVICTED = "interventions_evicted"
INTERVENTIONS_SUPPRESSED_DUPLICATE = "interventions_suppressed_duplicate"
INTERVENTIONS_DROPPED_STALE = "interventions_dropped_stale"
REEVALUATIONS_TRIGGERED = "justification_reevaluations"
REEVALUATIONS_ESCALATED = "justification_reevaluations_escalated"
#: Replies that read as a decision but could not be safely placed on an
#: action. Counted because "how often does the policy refuse?" is the number
#: that says whether the safety rule is well calibrated or merely strict.
RESOLUTIONS_REFUSED = "resolutions_refused_ambiguous"

#: Proposals that repeated an action a human had just decided. Counted
#: because a rising number would mean the echo window is too wide and real
#: re-proposals are being swallowed.
PROPOSALS_ECHOED = "proposals_echoing_a_decision"
RESOLUTIONS_APPLIED = "resolutions_applied"

TURNS_INGESTED = "turns_ingested"
TURNS_DEDUPED = "turns_deduplicated"
