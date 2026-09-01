"""
Injectable clock.

Two distinct notions of time, deliberately separated:

``now()``
    Wall-clock UTC. Used for *event* timestamps -- things that must be
    comparable against timestamps produced elsewhere (Agora transcript
    events, telemetry readings, the incident timeline).

``monotonic()``
    A steady, never-decreasing counter. Used for *durations* -- above all
    the Governor's rate-limit window. Wall clock can jump backwards (NTP
    correction, DST on a laptop, VM suspend/resume); a rate limiter built on
    it can be tricked into speaking twice inside its window, which is a hard
    red line (Quality Standard §4 #6). It must never be used for elapsed
    time.

Injecting the clock is also what makes the rate limiter and the staleness
rules deterministically testable, instead of tested with ``time.sleep``.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware, UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed point; never decreases."""
        ...


class SystemClock:
    """The production clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock under test control.

    Wall time and monotonic time advance together by default, but can be
    advanced independently so tests can reproduce a wall-clock jump without
    a corresponding monotonic jump.
    """

    __slots__ = ("_now", "_monotonic")

    def __init__(
        self,
        start: datetime | None = None,
        monotonic_start: float = 0.0,
    ) -> None:
        if start is None:
            start = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")
        self._now = start.astimezone(timezone.utc)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    # -- test controls ----------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Advance both wall and monotonic time."""
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += float(seconds)

    def advance_monotonic_only(self, seconds: float) -> None:
        self._monotonic += float(seconds)

    def set_wall_clock(self, moment: datetime) -> None:
        """Jump wall time without touching monotonic time -- models an NTP
        correction or a suspended laptop."""
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware time")
        self._now = moment.astimezone(timezone.utc)


SYSTEM_CLOCK: Clock = SystemClock()


def utc_now() -> datetime:
    """Convenience for the (few) places with no injected clock -- model
    default factories, primarily."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Canonical ISO-8601 rendering used on the wire and in the database.

    Always UTC, always with an explicit offset, so lexicographic ordering of
    the stored strings equals chronological ordering. The state store's
    timeline read path depends on that property.
    """
    if moment.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "+00:00")
