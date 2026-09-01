"""
Comparison policy for the risk engine.

There is exactly one definition in AEGIS of "do these two readings say the
same thing", and it lives here. The previous implementation had two -- an
exact ``==`` in the contradiction check and a ``±5.0`` band in the staleness
rules -- which meant the system could simultaneously believe a claim was
contradicted and not superseded. Both callers now share this object.

None of this is probabilistic. A tolerance band is a deterministic
threshold, not a confidence score; SSOT §26's non-goal on Bayesian modelling
is not touched by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Unit spellings that mean the same thing. Speech-to-text renders units
#: inconsistently ("percent", "%", "pct"), and a unit mismatch that is really
#: a spelling mismatch must not suppress a genuine contradiction.
_UNIT_ALIASES: dict[str, str] = {
    "%": "percent",
    "pct": "percent",
    "percent": "percent",
    "percentage": "percent",
    "ms": "milliseconds",
    "msec": "milliseconds",
    "millisecond": "milliseconds",
    "milliseconds": "milliseconds",
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "rps": "per_second",
    "qps": "per_second",
    "req/s": "per_second",
}


def normalise_unit(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    cleaned = unit.strip().lower()
    if not cleaned:
        return None
    return _UNIT_ALIASES.get(cleaned, cleaned)


@dataclass(frozen=True)
class MetricComparisonPolicy:
    """When do two readings of the same metric count as agreeing?

    Both a relative and an absolute band are applied, and agreement requires
    only one of them to hold. The absolute band matters for values near
    zero, where a relative band is meaninglessly tight; the relative band
    matters for large values, where a fixed band is meaninglessly loose.
    """

    relative_tolerance: float = 0.05
    absolute_tolerance: float = 1.0

    def __post_init__(self) -> None:
        if self.relative_tolerance < 0 or self.absolute_tolerance < 0:
            raise ValueError("tolerances must be non-negative")

    def values_agree(self, left: float, right: float) -> bool:
        difference = abs(left - right)
        if difference <= self.absolute_tolerance:
            return True
        scale = max(abs(left), abs(right))
        return difference <= self.relative_tolerance * scale

    def values_conflict(self, left: float, right: float) -> bool:
        return not self.values_agree(left, right)

    def units_comparable(self, left: Optional[str], right: Optional[str]) -> bool:
        """Two readings are only comparable if their units do not actively
        disagree.

        A missing unit is treated as comparable: people say "latency is
        about 200" and mean milliseconds. But an explicit ``seconds`` versus
        an explicit ``milliseconds`` is a genuine mismatch, and reporting a
        contradiction from it would be a false positive built on a unit bug.
        """
        left_norm = normalise_unit(left)
        right_norm = normalise_unit(right)
        if left_norm is None or right_norm is None:
            return True
        return left_norm == right_norm


@dataclass(frozen=True)
class StalenessPolicy:
    """When is a hypothesis no longer load-bearing?

    ``require_corroboration`` is the product's whole premise in one flag: an
    action justified by a hypothesis nobody ever corroborated is exactly the
    "hedged guess quietly became fact" failure mode (PRD §2.1). Leaving it on
    biases towards catching too much, which is the documented preference for
    the risk-detection chain (Quality Standard §9).
    """

    require_corroboration: bool = True
    metric: MetricComparisonPolicy = MetricComparisonPolicy()


DEFAULT_METRIC_POLICY = MetricComparisonPolicy()
DEFAULT_STALENESS_POLICY = StalenessPolicy()
