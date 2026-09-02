"""
HTTP surface.

Thin by intention. Every route does three things: validate input at the
boundary, hand it to the pipeline, and shape the response. No reasoning, no
state mutation, no risk decisions happen here -- which is what lets the whole
product be tested without starting a server.

The ingestion routes return ``202 Accepted``. Processing an utterance
involves an LLM call, and a browser relaying Agora's RTM stream cannot wait
on that: it has more transcripts arriving. Results reach the UI over the
event stream instead.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from backend.api.security import (
    SlidingWindowRateLimiter,
    client_key,
    read_json_body,
    require_token,
)
from backend.common.clock import SYSTEM_CLOCK, Clock
from backend.common.config import AppConfig, iter_config_summary, load_config
from backend.common.enums import (
    EvidenceSource,
    EvidenceSourceType,
    ExtractionCertainty,
    SourceModality,
)
from backend.common.errors import AegisError, ApiError
from backend.common.logging import configure_logging, get_logger, log_startup_banner
from backend.common.models import Evidence, TranscriptEvent, new_id
from backend.pipeline.events import EVENT_RESET
from backend.pipeline.factory import AegisRuntime, build_runtime
from backend.pipeline.worker import PipelineWorker

_log = get_logger("api")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def _json(payload: Any, status_code: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _error_response(exc: Exception) -> JSONResponse:
    """One error envelope for the whole API.

    Clients get a stable ``code`` they can branch on; unexpected exceptions
    return a generic message, because an internal error string can leak
    paths, queries or configuration.
    """
    if isinstance(exc, ApiError):
        return _json({"error": {"code": exc.code, "message": exc.message}}, exc.http_status)
    if isinstance(exc, AegisError):
        return _json({"error": {"code": exc.code, "message": exc.message}}, 400)
    _log.exception("unhandled error in an api route")
    return _json({"error": {"code": "internal_error", "message": "internal error"}}, 500)


class AegisApi:
    """Owns the runtime and the routes over it."""

    def __init__(
        self,
        runtime: AegisRuntime,
        *,
        worker: Optional[PipelineWorker] = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._runtime = runtime
        self._config = runtime.config
        self._clock = clock
        self._worker = worker or PipelineWorker(
            runtime.pipeline, max_depth=runtime.config.pipeline.ingest_queue_max_depth
        )
        self._limiter = SlidingWindowRateLimiter(runtime.config.api.ingest_rate_limit_per_minute)

    @property
    def worker(self) -> PipelineWorker:
        """The ingest worker. Exposed so a caller can wait for the queue to
        drain -- the only reliable completion signal for asynchronous ingest,
        and otherwise a race for anything that reads state after posting."""
        return self._worker

    # -- lifecycle --------------------------------------------------------

    async def on_startup(self) -> None:
        self._worker.start()
        log_startup_banner(
            _log, dict(iter_config_summary(self._config)), self._config.warnings()
        )

    async def on_shutdown(self) -> None:
        self._worker.stop()
        self._runtime.close()

    # -- read routes ------------------------------------------------------

    async def health(self, request: Request) -> Response:
        return _json(
            {
                "status": "ok",
                "incident_id": self._config.incident_id,
                "queue_depth": self._worker.depth,
                "processed": self._worker.processed,
                "dropped": self._worker.dropped,
                "extraction_provider": self._runtime.extraction.provider_name,
                "agora_authenticated": self._config.agora.is_authenticated,
                "rate_limit_seconds": self._runtime.governor.rate_limit_seconds,
                "window_open": self._runtime.governor.window_is_open(),
            }
        )

    async def state(self, request: Request) -> Response:
        """The full incident, with a conditional-GET short circuit.

        This is the hottest read in the system: the console re-reads it after
        every pipeline event, and an incident produces a burst of events per
        utterance. Serialising the entire incident each time -- every claim,
        every reading, the whole timeline -- to hand back something the client
        already has is the most wasteful thing this API could do, and it gets
        worse the longer the incident runs.

        So the store carries a monotonic version, it is exposed as an ETag,
        and an unchanged incident costs one integer comparison instead of a
        full projection. Standard HTTP, no client-side bookkeeping, and it
        degrades safely: the validator only ever over-invalidates.
        """
        version = self._runtime.store.version
        etag = f'W/"{self._config.incident_id}-{version}"'

        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

        view = self._runtime.store.incident_view(captured_at=self._clock.now())
        return _json(
            json.loads(view.model_dump_json()),
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    async def metrics(self, request: Request) -> Response:
        """What the system actually did, in numbers.

        Exposed as a first-class endpoint rather than left in the logs
        because every performance claim this backend makes should be
        checkable in one request: which stage is slow, how many provider
        calls were avoided and by which mechanism, and what the intervention
        scheduler chose to drop.
        """
        pipeline_metrics = self._runtime.metrics
        snapshot = pipeline_metrics.snapshot()
        return _json(
            {
                "incident_id": self._config.incident_id,
                "state_version": self._runtime.store.version,
                "stages": snapshot["stages"],
                "counters": snapshot["counters"],
                "extraction": {
                    **pipeline_metrics.derived(),
                    "cache_entries": self._runtime.extraction.cache_size,
                },
                "scheduling": self._runtime.governor.scheduling_stats(),
                "ingest": {
                    "queue_depth": self._worker.depth,
                    "processed": self._worker.processed,
                    "dropped": self._worker.dropped,
                },
            }
        )

    async def topology(self, request: Request) -> Response:
        return _json(self._runtime.topology.describe())

    async def telemetry(self, request: Request) -> Response:
        return _json({"metrics": list(self._runtime.telemetry.describe())})

    async def events(self, request: Request) -> Response:
        """Server-sent events: the UI's live feed.

        Written as a plain streaming response rather than pulling in an SSE
        library -- the protocol is four lines, and one fewer dependency on the
        demo path is worth more than the abstraction.
        """
        subscription = self._runtime.events.subscribe()

        async def stream():
            loop = asyncio.get_running_loop()
            try:
                yield b": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    event = await loop.run_in_executor(None, subscription.get, 1.0)
                    if event is None:
                        yield b": keepalive\n\n"  # keeps proxies from idling the connection out
                        continue
                    body = json.dumps(
                        {
                            "kind": event.kind,
                            "sequence": event.sequence,
                            "at": event.at,
                            **event.payload,
                        },
                        default=str,
                    )
                    yield f"event: {event.kind}\ndata: {body}\n\n".encode()
            finally:
                subscription.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- write routes -----------------------------------------------------

    async def ingest_transcript(self, request: Request) -> Response:
        """Accept one RTM transcript event, relayed by the browser client."""
        self._guard(request)
        payload = await read_json_body(request, max_bytes=self._config.api.max_body_bytes)

        try:
            event = TranscriptEvent.model_validate(
                {
                    "uid": payload.get("uid"),
                    "turn_id": payload.get("turn_id") or new_id(),
                    "role": payload.get("role", "human"),
                    "text": payload.get("text", ""),
                    "final": bool(payload.get("final", False)),
                    "timestamp": payload.get("timestamp") or self._clock.now(),
                    "source_modality": SourceModality.VOICE,
                }
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a 400, not a 500
            raise ApiError(f"invalid transcript event: {exc}") from exc

        # A relay that retries a dropped response resends the same turn id.
        # Answering that synchronously is a courtesy to the client and keeps
        # obvious duplicates out of the queue -- but it is only a report. The
        # authoritative claim happens in the pipeline, so every ingestion
        # path is covered by it and not just this one.
        if self._runtime.store.turn_seen(event.turn_id):
            _log.info("duplicate turn ignored", turn_id=event.turn_id, uid=event.uid)
            return _json(
                {"accepted": True, "turn_id": event.turn_id, "duplicate": True},
                200,
            )

        accepted = self._worker.submit(event)
        return _json(
            {"accepted": True, "turn_id": event.turn_id, "duplicate": False,
             "queue_depth": self._worker.depth, "displaced_older_event": not accepted},
            202,
        )

    async def ingest_text(self, request: Request) -> Response:
        """The typed side-channel.

        Wrapped into the same transcript-event shape as speech, so it takes
        the identical path through extraction, state and risk. Text is a
        second producer of an existing contract, not a second pipeline.
        """
        self._guard(request)
        payload = await read_json_body(request, max_bytes=self._config.api.max_body_bytes)

        text = str(payload.get("text", "")).strip()
        if not text:
            raise ApiError("text is required")

        event = TranscriptEvent(
            uid=str(payload.get("uid") or "text-client"),
            turn_id=str(payload.get("turn_id") or new_id()),
            role="human",
            text=text,
            final=True,
            timestamp=self._clock.now(),
            source_modality=SourceModality.TEXT,
        )
        self._worker.submit(event)
        return _json({"accepted": True, "turn_id": event.turn_id}, 202)

    async def ingest_evidence(self, request: Request) -> Response:
        """Submit a reading observed outside the voice channel.

        The reading is what enters the system, not the image: AEGIS reasons
        over ``metric_name`` and ``value``, and a screenshot is one way to
        obtain them. ``extraction_certainty`` is a categorical high/low flag,
        never a probability -- low-certainty evidence can prompt a question
        but can never, by itself, produce a warning.
        """
        self._guard(request)
        payload = await read_json_body(request, max_bytes=self._config.api.max_body_bytes)

        metric_name = str(payload.get("metric_name", "")).strip()
        if not metric_name:
            raise ApiError("metric_name is required")
        if "value" not in payload:
            raise ApiError("value is required")

        certainty = str(payload.get("extraction_certainty", "high")).lower()
        if certainty not in {"high", "low"}:
            raise ApiError("extraction_certainty must be 'high' or 'low'")

        try:
            evidence = Evidence(
                source_type=EvidenceSourceType.VISUAL,
                source=EvidenceSource.SCREENSHOT_UPLOAD,
                metric_name=metric_name,
                value=payload["value"],
                unit=payload.get("unit"),
                extraction_certainty=ExtractionCertainty(certainty),
                uploader_uid=str(payload.get("uploader_uid") or "text-client"),
                timestamp=self._clock.now(),
                target_ref=payload.get("target_ref"),
                raw_reference=payload.get("raw_reference"),
            )
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"invalid evidence: {exc}") from exc

        decision = self._runtime.pipeline.ingest_evidence(evidence)
        return _json(
            {
                "accepted": True,
                "evidence_id": evidence.evidence_id,
                "intervened": bool(decision and decision.should_speak),
                "spoken_text": decision.spoken_text if decision else None,
            },
            202,
        )

    async def reset(self, request: Request) -> Response:
        """Start a fresh incident.

        A demo affordance, and the one that matters most: the highest-
        probability failure in a live demo is being asked to run it again,
        because the default database is a file and the second run inherits
        the first one's pending actions, spent turn ids and -- worst -- the
        governor's closed rate-limit window, which makes AEGIS mute.

        Guarded like every other mutating endpoint. It destroys the current
        incident's record, so it is deliberately explicit rather than a side
        effect of anything else.
        """
        self._guard(request)
        self._runtime.reset()
        _log.info("incident reset via api", incident_id=self._config.incident_id)
        self._runtime.events.publish(EVENT_RESET, incident_id=self._config.incident_id)
        return _json(
            {
                "reset": True,
                "incident_id": self._config.incident_id,
                "state_version": self._runtime.store.version,
            }
        )

    async def set_metric(self, request: Request) -> Response:
        """Move a mocked metric, for rehearsal.

        Explicitly a demo affordance: it changes what the *mock* reports, and
        cannot touch any real system. Guarded like every other mutating
        endpoint because it can change what AEGIS says.
        """
        self._guard(request)
        payload = await read_json_body(request, max_bytes=self._config.api.max_body_bytes)
        metric = str(payload.get("metric_name", ""))
        if "value" not in payload:
            raise ApiError("value is required")
        self._runtime.telemetry.set_value(metric, payload["value"])
        return _json({"metric_name": metric, "value": payload["value"]})

    # -- internals --------------------------------------------------------

    def _guard(self, request: Request) -> None:
        require_token(request, self._config.api)
        self._limiter.check(client_key(request))

    # -- assembly ---------------------------------------------------------

    def build(self) -> Starlette:
        routes = [
            Route("/api/health", _guarded(self.health), methods=["GET"]),
            Route("/api/state", _guarded(self.state), methods=["GET"]),
            Route("/api/topology", _guarded(self.topology), methods=["GET"]),
            Route("/api/telemetry", _guarded(self.telemetry), methods=["GET"]),
            Route("/api/metrics", _guarded(self.metrics), methods=["GET"]),
            Route("/api/events", _guarded(self.events), methods=["GET"]),
            Route("/api/transcript", _guarded(self.ingest_transcript), methods=["POST"]),
            Route("/api/text", _guarded(self.ingest_text), methods=["POST"]),
            Route("/api/evidence", _guarded(self.ingest_evidence), methods=["POST"]),
            Route("/api/telemetry/set", _guarded(self.set_metric), methods=["POST"]),
            Route("/api/reset", _guarded(self.reset), methods=["POST"]),
        ]

        if FRONTEND_DIR.is_dir():
            # Two experiences, one origin, one process.
            #
            #   /          the landing page -- what AEGIS is, and why the
            #              boundary between interpretation, authorization and
            #              execution is the entire product. Explanatory
            #              content plus four read-only API reads.
            #   /command   the operator console -- the thing that is live
            #              during an incident, reading /api/state for
            #              authoritative state and /api/events for hints.
            #
            # Serving both from this process is what lets the landing page
            # read live topology, telemetry, health and metrics from the API
            # instead of shipping a second copy of them and quietly drifting
            # from the system it is describing.
            landing = FRONTEND_DIR / "landing.html"
            console = FRONTEND_DIR / "index.html"

            def _page(path: Path):
                # A factory, not an inline lambda: a lambda closing over a
                # loop or over a name reassigned below binds late and every
                # route ends up serving the last file.
                return lambda _request: FileResponse(path)

            if landing.is_file():
                routes.append(Route("/", _page(landing), methods=["GET"]))
                # ``/hero`` was the walkthrough's own route before the landing
                # page absorbed it. Redirect rather than 404: it is in the
                # README, and anything already linked at it should still land
                # somewhere real.
                routes.append(
                    Route(
                        "/hero",
                        lambda _request: RedirectResponse("/", status_code=308),
                        methods=["GET"],
                    )
                )
            else:
                # No landing page shipped: the console is still the product,
                # so it keeps the root rather than the origin serving nothing.
                routes.append(Route("/", _page(console), methods=["GET"]))

            routes.append(Route("/command", _page(console), methods=["GET"]))
            routes.append(Mount("/static", app=StaticFiles(directory=str(FRONTEND_DIR))))

        middleware = []
        if self._config.api.cors_allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=list(self._config.api.cors_allow_origins),
                    allow_methods=["GET", "POST"],
                    allow_headers=["authorization", "content-type"],
                )
            )

        # Starlette 1.x drops on_startup/on_shutdown in favour of a lifespan
        # context manager, which is also the shape that guarantees shutdown
        # runs even when startup raised part-way through.
        @asynccontextmanager
        async def lifespan(_app: Starlette):
            await self.on_startup()
            try:
                yield
            finally:
                await self.on_shutdown()

        return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def _guarded(handler):
    """Wrap a route so every failure becomes the same error envelope."""

    async def route(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    return route


def create_app(
    config: Optional[AppConfig] = None,
    *,
    runtime: Optional[AegisRuntime] = None,
) -> Starlette:
    config = config or load_config()
    configure_logging(config.log_level, log_file=config.log_file)
    runtime = runtime or build_runtime(config)
    return AegisApi(runtime).build()
