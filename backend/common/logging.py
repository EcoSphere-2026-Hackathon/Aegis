"""
Structured logging.

One JSON object per line, one event per pipeline stage. This format is not
just for debugging: the Blueprint makes it the *input to the Phase 2
evaluation harness*, so it is defined once, deliberately, and reused rather
than being rewritten later ("don't build a throwaway logger").

The standard it has to meet (Quality Standard §14) is that a rehearsal can
be fully reconstructed from the log alone -- what was said, what was
extracted, what state changed, why the verdict came out as it did, why
AEGIS spoke or stayed silent, what the human confirmed, how long each stage
took, and what failed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from backend.common.config import redact_mapping

# The turn currently being processed. Set once per ingested transcript event
# and read by every downstream stage, so an entire causal chain -- utterance
# through intervention -- can be filtered out of the log by one id.
_correlation_id: ContextVar[Optional[str]] = ContextVar("aegis_correlation_id", default=None)

LOGGER_NAME = "aegis"

#: Stage names, fixed so the evaluation harness can rely on them.
STAGE_TRANSCRIPT_RECEIVED = "transcript_received"
STAGE_CLAIM_EXTRACTED = "claim_extracted"
STAGE_CLAIM_REJECTED = "claim_rejected"
STAGE_STATE_MUTATED = "state_mutated"
STAGE_RISK_EVALUATED = "risk_evaluated"
STAGE_GOVERNOR_DECIDED = "governor_decided"
STAGE_SPEAK_CALLED = "speak_called"
STAGE_HUMAN_RESOLUTION = "human_resolution"
STAGE_EVIDENCE_INGESTED = "evidence_ingested"
STAGE_FAILURE = "failure"


class JsonFormatter(logging.Formatter):
    """Renders records as single-line JSON with redacted context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation = getattr(record, "correlation_id", None) or _correlation_id.get()
        if correlation:
            payload["correlation_id"] = correlation

        stage = getattr(record, "stage", None)
        if stage:
            payload["stage"] = stage

        context = getattr(record, "context", None)
        if context:
            payload["context"] = redact_mapping(context)

        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is not None:
            payload["duration_ms"] = round(float(duration_ms), 3)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, default=_json_default, ensure_ascii=False)
        except (TypeError, ValueError):
            # A logger that raises while logging a failure is worse than a
            # lossy log line. Degrade, never throw.
            return json.dumps(
                {
                    "ts": payload["ts"],
                    "level": "ERROR",
                    "logger": record.name,
                    "message": "log record was not serialisable",
                    "original_message": str(record.getMessage())[:500],
                }
            )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return str(value.value)
    return repr(value)


def configure_logging(
    level: str = "INFO",
    *,
    log_file: Optional[Path] = None,
    stream: Any = None,
) -> logging.Logger:
    """Idempotent logging setup. Safe to call from tests repeatedly."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = JsonFormatter()

    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(component: str) -> "ComponentLogger":
    return ComponentLogger(logging.getLogger(f"{LOGGER_NAME}.{component}"))


class ComponentLogger:
    """Thin wrapper that makes the structured fields the *only* ergonomic
    way to log, so nobody falls back to f-string prose the harness cannot
    parse."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _emit(
        self,
        level: int,
        message: str,
        *,
        stage: Optional[str] = None,
        duration_ms: Optional[float] = None,
        exc_info: Any = None,
        **context: Any,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra: dict[str, Any] = {}
        if stage:
            extra["stage"] = stage
        if context:
            extra["context"] = context
        if duration_ms is not None:
            extra["duration_ms"] = duration_ms
        correlation = _correlation_id.get()
        if correlation:
            extra["correlation_id"] = correlation
        self._logger.log(level, message, extra=extra, exc_info=exc_info)

    def debug(self, message: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        kwargs.setdefault("stage", STAGE_FAILURE)
        self._emit(logging.ERROR, message, **kwargs)

    @contextmanager
    def timed(self, message: str, *, stage: str, **context: Any) -> Iterator[dict[str, Any]]:
        """Times a stage and logs its duration, whether or not it succeeded.

        The returned dict can be updated by the caller to attach fields
        discovered during the block (e.g. how many claims came back).
        """
        started = time.perf_counter()
        extra_context: dict[str, Any] = {}
        try:
            yield extra_context
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._emit(
                logging.ERROR,
                message,
                stage=STAGE_FAILURE,
                duration_ms=elapsed_ms,
                failure_type=type(exc).__name__,
                failure_code=getattr(exc, "code", None),
                **context,
                **extra_context,
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._emit(
                logging.INFO,
                message,
                stage=stage,
                duration_ms=elapsed_ms,
                **context,
                **extra_context,
            )


@contextmanager
def correlation_scope(correlation_id: Optional[str] = None) -> Iterator[str]:
    """Binds a correlation id for the duration of one ingested turn."""
    value = correlation_id or str(uuid.uuid4())
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def current_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def log_startup_banner(logger: ComponentLogger, summary: Mapping[str, Any], warnings: tuple[str, ...]) -> None:
    logger.info("aegis starting", stage="startup", **dict(summary))
    for warning in warnings:
        logger.warning("configuration warning", stage="startup", detail=warning)
