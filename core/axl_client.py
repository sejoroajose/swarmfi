"""
core/axl_client.py
Async client for a single AXL node's local HTTP bridge.

Wraps the three endpoints the swarm uses:
  GET  /topology  → NodeInfo
  POST /send      → send a SwarmMessage to a peer
  GET  /recv      → poll for one inbound message

Retry logic uses tenacity with exponential back-off so transient
node hiccups don't crash agents.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.schema import SwarmMessage

log = structlog.get_logger(__name__)

# How long to wait between /recv polls when the queue is empty
_RECV_POLL_INTERVAL = 0.25  # seconds
_REQUEST_TIMEOUT    = 10.0  # seconds


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NodeInfo:
    public_key: str
    ipv6:       str
    api_url:    str


@dataclass(frozen=True)
class ReceivedMessage:
    message: SwarmMessage
    from_pubkey: str


# ── Client ────────────────────────────────────────────────────────────────────


class AXLClient:
    """
    Async client for one AXL node.

    Usage:
        async with AXLClient("http://127.0.0.1:9002") as client:
            info = await client.topology()
            await client.send(dest_pubkey, swarm_message)
            msg = await client.recv()
    """

    def __init__(self, api_url: str, agent_name: str = "") -> None:
        self._api_url = api_url.rstrip("/")
        self._agent_name = agent_name
        self._http: httpx.AsyncClient | None = None
        self._log = log.bind(agent=agent_name, api=self._api_url)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "AXLClient":
        self._http = httpx.AsyncClient(
            base_url=self._api_url,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("AXLClient must be used as an async context manager")
        return self._http

    # ── retry helper ──────────────────────────────────────────────────────────

    def _retrying(self) -> AsyncRetrying:
        """Shared retry policy: 3 attempts, exponential back-off."""
        return AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        )

    # ── public API ────────────────────────────────────────────────────────────

    async def topology(self) -> NodeInfo:
        """
        GET /topology — returns this node's public key and IPv6 address.
        Called at startup to discover our own identity.
        """
        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.get("/topology")
                resp.raise_for_status()

        data = resp.json()
        info = NodeInfo(
            public_key=data["our_public_key"],
            ipv6=data["our_ipv6"],
            api_url=self._api_url,
        )
        self._log.debug("topology fetched", pubkey=info.public_key[:16] + "…")
        return info

    async def is_healthy(self) -> bool:
        """Quick liveness check — does not raise."""
        try:
            await self.topology()
            return True
        except Exception:
            return False

    async def send(self, dest_pubkey: str, message: SwarmMessage) -> None:
        """
        POST /send — deliver message bytes to a peer by their public key.
        AXL handles routing; we just set the destination header.
        """
        payload_bytes = message.encode()
        headers = {"X-Destination-Peer-Id": dest_pubkey}

        async for attempt in self._retrying():
            with attempt:
                resp = await self._client.post(
                    "/send",
                    content=payload_bytes,
                    headers=headers,
                )
                resp.raise_for_status()

        self._log.info(
            "message sent",
            type=message.message_type,
            dest=dest_pubkey[:16] + "…",
            msg_id=message.message_id,
        )

    async def recv(self, timeout: float | None = None) -> ReceivedMessage | None:
        """
        GET /recv — poll for the next inbound message.
        Returns None if the queue is empty (204 or empty body).
        Raises ValueError if the body is not a valid SwarmMessage.
        """
        try:
            resp = await self._client.get("/recv", timeout=timeout or _REQUEST_TIMEOUT)
        except httpx.TimeoutException:
            return None

        if resp.status_code == 204 or not resp.content:
            return None

        resp.raise_for_status()
        from_key = resp.headers.get("X-From-Peer-Id", "")
        raw = resp.content

        try:
            msg = SwarmMessage.decode(raw)
        except Exception as exc:
            self._log.warning("recv: failed to decode message", error=str(exc))
            raise ValueError(f"Invalid SwarmMessage from {from_key!r}: {exc}") from exc

        self._log.info(
            "message received",
            type=msg.message_type,
            from_=from_key[:16] + "…",
            msg_id=msg.message_id,
        )
        return ReceivedMessage(message=msg, from_pubkey=from_key)

    async def recv_stream(self) -> AsyncIterator[ReceivedMessage]:
        """
        Continuous async generator — yields messages as they arrive.
        Polls /recv at _RECV_POLL_INTERVAL when the queue is empty.
        Run this in a background task.

        Example:
            async for received in client.recv_stream():
                await handle(received.message)
        """
        while True:
            received = await self.recv()
            if received is not None:
                yield received
            else:
                await asyncio.sleep(_RECV_POLL_INTERVAL)