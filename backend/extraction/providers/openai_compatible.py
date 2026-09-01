"""
Provider for any OpenAI-compatible ``/chat/completions`` endpoint.

Chosen as the vendor surface because it is the one shape supported by
OpenAI, Groq, Together, OpenRouter, Ollama, vLLM and most others -- so the
LLM vendor decision, which the SSOT leaves explicitly open, stays a
configuration change rather than a code change. The provider is flagged in
the startup log so the team always knows which one produced a run's claims.

The provider does not retry. Retry policy belongs to the extraction service,
which owns the bound and the degradation behaviour; duplicating it here
would give the pipeline two multiplying retry budgets and an unbounded worst
case on the critical path.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from backend.common.config import LlmConfig
from backend.common.errors import ProviderError
from backend.extraction.contracts import ProviderRequest, ProviderResponse


class OpenAICompatibleProvider:
    """Structured extraction over an OpenAI-compatible chat endpoint."""

    name = "openai_compatible"

    def __init__(self, config: LlmConfig, *, client: Optional[httpx.Client] = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.request_timeout_seconds),
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def supports_vision(self) -> bool:
        return self._config.supports_vision

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if not self._config.api_key:
            raise ProviderError("no API key configured for the extraction provider", provider=self.name)

        payload: dict[str, Any] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        }

        started = time.perf_counter()
        try:
            response = self._client.post(
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._config.api_key.reveal()}"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "extraction provider timed out",
                provider=self.name,
                timeout_seconds=self._config.request_timeout_seconds,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "extraction provider transport error", provider=self.name, detail=str(exc)
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0

        if response.status_code >= 400:
            # The body can echo the prompt; never let it into an error
            # context that will be logged.
            raise ProviderError(
                "extraction provider returned an error status",
                provider=self.name,
                status_code=response.status_code,
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "extraction provider returned an unrecognised envelope", provider=self.name
            ) from exc

        return ProviderResponse(
            raw_text=content or "",
            model=str(body.get("model", self._config.model)),
            provider=self.name,
            latency_ms=latency_ms,
        )
