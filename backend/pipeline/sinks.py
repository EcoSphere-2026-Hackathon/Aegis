"""
Where an intervention actually goes.

The pipeline decides *what* to say; a sink decides *how it is delivered*.
Keeping them apart is what lets the entire reasoning loop be tested end to
end with no Agora account, and lets the live demo swap in the real voice
path without touching a line of reasoning code.

A sink raises on failure. It never swallows an error into a silent no-op:
an intervention that was decided but never heard is, for this product,
indistinguishable from a missed intervention, so the caller must find out
and return the rate-limit window.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.errors import InterventionError
from backend.common.logging import STAGE_SPEAK_CALLED, get_logger

_log = get_logger("intervention")


@runtime_checkable
class InterventionSink(Protocol):
    name: str

    def speak(self, text: str) -> None:
        """Deliver spoken text. Raises :class:`InterventionError` on failure."""
        ...


@dataclass
class SpokenLine:
    text: str
    at: str


class RecordingSink:
    """Captures interventions instead of speaking them.

    Used by the text-only pipeline, the golden-demo replay and every test.
    """

    name = "recording"

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK, echo: bool = False) -> None:
        self._clock = clock
        self._echo = echo
        self._lock = threading.Lock()
        self._lines: list[SpokenLine] = []

    def speak(self, text: str) -> None:
        with self._lock:
            self._lines.append(SpokenLine(text=text, at=self._clock.now().isoformat()))
        _log.info("intervention recorded", stage=STAGE_SPEAK_CALLED, sink=self.name, characters=len(text))
        if self._echo:
            print(f"\n  AEGIS: {text}\n")

    @property
    def lines(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(line.text for line in self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class FailingSink:
    """A sink that always fails -- for exercising the delivery-failure path."""

    name = "failing"

    def __init__(self, message: str = "simulated delivery failure") -> None:
        self._message = message

    def speak(self, text: str) -> None:
        raise InterventionError(self._message, sink=self.name)


class CompositeSink:
    """Delivers to several sinks, keeping a record even when voice is live.

    Failure of the *primary* sink is a real failure; failure of a secondary
    recorder is not allowed to mask it, so the primary's exception wins.
    """

    name = "composite"

    def __init__(self, primary: InterventionSink, *secondaries: InterventionSink) -> None:
        self._primary = primary
        self._secondaries = secondaries

    def speak(self, text: str) -> None:
        for secondary in self._secondaries:
            try:
                secondary.speak(text)
            except Exception:  # noqa: BLE001 - a recorder must never mask the real path
                _log.exception("secondary intervention sink failed", sink=getattr(secondary, "name", "?"))
        self._primary.speak(text)
