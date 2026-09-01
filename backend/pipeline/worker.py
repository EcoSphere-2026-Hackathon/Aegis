"""
The ingestion queue and its worker.

Transcript events arrive from a browser relaying Agora's RTM stream. That
relay must never wait on an LLM call: the blueprint is explicit that the
ingestion loop is not to be blocked on a stuck provider, and an HTTP handler
that blocks is exactly how that happens.

So ingestion is a bounded queue plus one worker thread. One, not a pool --
processing is ordered by design (a confirmation must be handled after the
action it confirms), and the state store serialises writes anyway, so extra
workers would add contention and non-determinism for no throughput.

The queue is bounded because an unbounded one does not fail, it just grows
until the process dies. When full, the *oldest* event is dropped and the
drop is logged: during a burst, the freshest utterance is the one that still
matters.
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

from backend.common.logging import STAGE_FAILURE, STAGE_TRANSCRIPT_RECEIVED, get_logger
from backend.common.models import TranscriptEvent
from backend.pipeline.orchestrator import IncidentPipeline

_log = get_logger("worker")

#: How long the worker waits for the queue before re-checking the stop flag.
_POLL_SECONDS = 0.25


class PipelineWorker:
    """Runs the pipeline off the request thread."""

    def __init__(self, pipeline: IncidentPipeline, *, max_depth: int = 256) -> None:
        self._pipeline = pipeline
        self._queue: queue.Queue[TranscriptEvent] = queue.Queue(maxsize=max_depth)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._processed = 0
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aegis-pipeline", daemon=True)
        self._thread.start()
        _log.info("pipeline worker started")

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        _log.info("pipeline worker stopped", processed=self._processed, dropped=self._dropped)

    def __enter__(self) -> "PipelineWorker":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- submission -------------------------------------------------------

    def submit(self, event: TranscriptEvent) -> bool:
        """Enqueue an event. Returns False if an older event had to be dropped.

        Never blocks the caller: an HTTP handler waiting on a full queue is
        an HTTP handler holding a connection open while the incident moves on.
        """
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            pass

        try:
            dropped = self._queue.get_nowait()
            with self._lock:
                self._dropped += 1
            _log.warning(
                "ingest queue full; dropped the oldest event",
                stage=STAGE_TRANSCRIPT_RECEIVED,
                dropped_turn_id=dropped.turn_id,
                dropped_uid=dropped.uid,
                depth=self._queue.qsize(),
            )
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(event)
        except queue.Full:  # pragma: no cover - only under pathological contention
            with self._lock:
                self._dropped += 1
            return False
        return False

    # -- stats ------------------------------------------------------------

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def processed(self) -> int:
        with self._lock:
            return self._processed

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def drain(self, *, timeout: float = 5.0) -> bool:
        """Block until the queue is empty. For tests and orderly shutdown."""
        deadline = threading.Event()
        waited = 0.0
        while self._queue.unfinished_tasks and waited < timeout:
            deadline.wait(0.01)
            waited += 0.01
        return not self._queue.unfinished_tasks

    # -- loop -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                self._pipeline.handle_transcript(event)
                with self._lock:
                    self._processed += 1
            except Exception:  # noqa: BLE001 - the worker outlives any single turn
                _log.exception(
                    "worker failed on an event", stage=STAGE_FAILURE, turn_id=event.turn_id
                )
            finally:
                self._queue.task_done()
