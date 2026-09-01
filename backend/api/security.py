"""
API guards.

The threat model here is narrow and specific. AEGIS has no user accounts and
no multi-tenancy, so there is nothing resembling conventional authentication
to build. But the ingestion endpoints accept ``confirmation`` claims, and a
confirmation is what turns a proposed action into an authorised one. An
unauthenticated endpoint that accepts one is a direct path to the system's
single worst failure -- an action treated as authorised that no human
approved.

So: a shared bearer token on every mutating endpoint, compared in constant
time, plus a bounded request rate so a runaway client cannot flood the
ingest queue during a demo. That is the proportionate control. Anything more
(user accounts, RBAC, sessions) would be scope the product explicitly does
not have.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import deque
from typing import Optional

from starlette.requests import Request

from backend.common.config import ApiConfig
from backend.common.errors import (
    PayloadTooLargeError,
    RateLimitedError,
    UnauthorizedError,
)


def require_token(request: Request, config: ApiConfig) -> None:
    """Check the bearer token on a mutating request.

    When no token is configured the check is skipped -- that is the local
    development path, and the startup banner warns about it loudly rather
    than letting it pass unnoticed.
    """
    if not config.auth_enabled:
        return

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise UnauthorizedError("missing bearer token")

    # Constant-time comparison. The attack is unlikely over a LAN demo, but
    # a timing-safe compare costs nothing and removes the question.
    if not hmac.compare_digest(presented.strip(), config.ingest_token.reveal()):
        raise UnauthorizedError("invalid bearer token")


class SlidingWindowRateLimiter:
    """Per-client request ceiling over a rolling minute.

    Deliberately simple and in-process: there is one server, one incident and
    a handful of clients. Its job is to stop a looping script from filling
    the ingest queue mid-demo, not to withstand an adversary.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._window_seconds = 60.0
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, client_key: str, *, now: Optional[float] = None) -> None:
        moment = now if now is not None else time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(client_key, deque())
            cutoff = moment - self._window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                retry_after = max(0.0, self._window_seconds - (moment - hits[0]))
                raise RateLimitedError(
                    "too many requests",
                    retry_after_seconds=round(retry_after, 2),
                    limit_per_minute=self._max,
                )
            hits.append(moment)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


async def read_json_body(request: Request, *, max_bytes: int) -> dict:
    """Read and parse a JSON body under a hard size ceiling.

    The ceiling is enforced on the bytes actually read rather than trusting
    ``Content-Length``, which a client controls.
    """
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise PayloadTooLargeError("request body too large", max_bytes=max_bytes)
        chunks.append(chunk)

    raw = b"".join(chunks)
    if not raw:
        return {}

    import json

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        from backend.common.errors import ApiError

        raise ApiError("request body was not valid JSON") from exc

    if not isinstance(payload, dict):
        from backend.common.errors import ApiError

        raise ApiError("request body must be a JSON object")
    return payload


def client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"
