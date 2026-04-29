"""
core/compute/client.py
0G Compute Network inference client.

0G Compute exposes an OpenAI-compatible API.
Base URL per provider: https://<provider-host>/v1/proxy
Auth: Authorization: Bearer app-sk-<YOUR_SECRET>

Get your key:
  1. Go to https://0g.ai/compute-network
  2. Find a provider (GLM-5-FP8 or qwen3)
  3. Transfer 0G funds to provider
  4. Click Generate New Key → app-sk-...

Env vars:
  ZG_COMPUTE_BASE_URL  e.g. https://compute-network-1.integratenetwork.work/v1/proxy
  ZG_COMPUTE_API_KEY   app-sk-...
  ZG_COMPUTE_MODEL     zai-org/GLM-5-FP8  (default)
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import structlog
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

_DEFAULT_MODEL   = "zai-org/GLM-5-FP8"
_DEFAULT_TIMEOUT = 60.0


class _MockComputeBackend:
    """Deterministic mock — returns structured JSON risk assessments."""

    async def chat(self, messages: list[dict], model: str, **kw: Any) -> str:
        # Extract context from the last user message to give a realistic mock
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Return a valid JSON risk assessment
        return json.dumps({
            "risk_score": 3.2,
            "action": "buy",
            "confidence": 0.78,
            "reasoning": "Mock: signal strength is medium, price action consistent with trend",
            "rejection_reason": None,
        })


class _LiveComputeBackend:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key  = api_key
        self._model    = model
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_LiveComputeBackend":
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    async def chat(self, messages: list[dict], model: str | None = None, **kw: Any) -> str:
        assert self._http
        payload = {
            "model":    model or self._model,
            "messages": messages,
            "max_tokens": kw.get("max_tokens", 512),
        }
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class ZeroGComputeClient:
    """
    0G Compute inference client.
    Auto-selects mock when ZG_COMPUTE_API_KEY is not set.
    """

    def __init__(self, backend: _LiveComputeBackend | _MockComputeBackend) -> None:
        self._backend = backend
        self.is_live  = isinstance(backend, _LiveComputeBackend)

    @classmethod
    def from_env(cls) -> "ZeroGComputeClient":
        api_key  = os.getenv("ZG_COMPUTE_API_KEY", "").strip()
        base_url = os.getenv("ZG_COMPUTE_BASE_URL",
                             "https://compute-network-1.integratenetwork.work/v1/proxy")
        model    = os.getenv("ZG_COMPUTE_MODEL", _DEFAULT_MODEL)

        if api_key:
            log.info("0G Compute: live mode", model=model)
            return cls(_LiveComputeBackend(base_url, api_key, model))
        log.info("0G Compute: mock mode (set ZG_COMPUTE_API_KEY for live)")
        return cls(_MockComputeBackend())

    async def __aenter__(self) -> "ZeroGComputeClient":
        if isinstance(self._backend, _LiveComputeBackend):
            await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if isinstance(self._backend, _LiveComputeBackend):
            await self._backend.__aexit__(*args)

    async def chat(self, messages: list[dict], model: str | None = None, **kw: Any) -> str:
        return await self._backend.chat(messages, model=model, **kw)