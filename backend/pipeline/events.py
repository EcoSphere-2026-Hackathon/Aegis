"""
In-process event bus.

The UI needs to see state change as it happens, and the pipeline must not
know or care that a UI exists. A subscriber that is slow, wedged, or has
simply closed its browser tab must never slow down incident processing --
so every subscriber gets a bounded queue and is dropped from rather than
blocking the publisher.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from backend.common.clock import SYSTEM_CLOCK, Clock

#: Per-subscriber buffer. Large enough to absorb a burst while a browser
#: reconnects, small enough that a dead subscriber cannot pin memory.
SUBSCRIBER_QUEUE_SIZE = 256


@dataclass(frozen=True)
class PipelineEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    at: Optional[str] = None


class EventBus:
    """Fan-out to zero or more live subscribers."""

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._sequence = 0

    def publish(self, kind: str, **payload: Any) -> PipelineEvent:
        with self._lock:
            self._sequence += 1
            event = PipelineEvent(
                kind=kind,
                payload=payload,
                sequence=self._sequence,
                at=self._clock.now().isoformat(),
            )
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # The subscriber is not keeping up. Dropping its oldest event
                # is strictly better than blocking the incident pipeline on a
                # browser tab.
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event

    def subscribe(self) -> "Subscription":
        channel: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.add(channel)
        return Subscription(self, channel)

    def _unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class Subscription:
    """A single consumer's view of the bus. Use as a context manager so the
    subscription is always released, including on client disconnect."""

    def __init__(self, bus: EventBus, channel: queue.Queue) -> None:
        self._bus = bus
        self._channel = channel
        self._closed = False

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self._channel)  # noqa: SLF001 - intentional friend access

    def get(self, timeout: Optional[float] = None) -> Optional[PipelineEvent]:
        try:
            return self._channel.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[PipelineEvent]:
        events: list[PipelineEvent] = []
        while True:
            try:
                events.append(self._channel.get_nowait())
            except queue.Empty:
                return events

    def __iter__(self) -> Iterator[PipelineEvent]:
        while not self._closed:
            event = self.get(timeout=1.0)
            if event is not None:
                yield event


# Event kinds, fixed so the frontend and the tests agree on one vocabulary.
EVENT_TRANSCRIPT = "transcript"
EVENT_CLAIM = "claim"
EVENT_STATE_CHANGED = "state_changed"
EVENT_RISK_VERDICT = "risk_verdict"
EVENT_INTERVENTION = "intervention"

#: The incident was cleared for a fresh run. The console listens for this
#: so a reset from the API is visible without a page reload.
EVENT_RESET = "reset"
EVENT_RESOLUTION = "resolution"
EVENT_EVIDENCE = "evidence"
EVENT_ERROR = "error"
