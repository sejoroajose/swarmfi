"""
agents/base.py
Abstract base class for all SwarmFi agents.

Provides:
  - AXL client lifecycle (start / stop)
  - Identity discovery via /topology
  - Message dispatch loop (recv_stream → on_message)
  - Built-in PING / PONG handling
  - Graceful shutdown on SIGTERM / SIGINT
"""

from __future__ import annotations

import asyncio
import signal
import time
from abc import ABC, abstractmethod

import structlog

from core.axl_client import AXLClient, ReceivedMessage
from core.registry import AgentRegistry
from core.schema import (
    AgentRole,
    MessageType,
    SwarmMessage,
    make_pong,
)

log = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """
    Subclass this and implement on_message().
    Call await agent.run() to start the event loop.
    """

    def __init__(self, role: AgentRole, registry: AgentRegistry) -> None:
        self.role     = role
        self.registry = registry
        self._log     = log.bind(agent=role.value)

        api_url = registry.api_url_for(role)
        self._client = AXLClient(api_url, agent_name=role.value)

        self._pubkey: str = ""
        self._running       = False
        self._dispatch_task: asyncio.Task[None] | None = None

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def pubkey(self) -> str:
        if not self._pubkey:
            raise RuntimeError("Agent not started — pubkey not yet known")
        return self._pubkey

    async def start(self) -> None:
        """Open AXL client, discover identity, start dispatch loop."""
        await self._client.__aenter__()
        info = await self._client.topology()
        self._pubkey = info.public_key
        self._running = True
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name=f"{self.role.value}-dispatch"
        )
        self._log.info("agent started", pubkey=self._pubkey[:16] + "…")

    async def stop(self) -> None:
        """Gracefully shut down the dispatch loop and close HTTP client."""
        self._running = False
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        await self._client.__aexit__(None, None, None)
        self._log.info("agent stopped")

    async def run(self) -> None:
        """
        Entry point: start the agent, register OS signal handlers,
        and block until stop() is called.
        """
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            self._log.info("shutdown signal received")
            loop.create_task(self.stop())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown)

        await self.start()
        # Block until stop() is called
        while self._running:
            await asyncio.sleep(0.1)

    async def send(self, dest_role: AgentRole, message: SwarmMessage) -> None:
        """Send a SwarmMessage to another agent by role."""
        dest_pubkey = self.registry.pubkey_for(dest_role)
        await self._client.send(dest_pubkey, message)

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        """Poll /recv and route each message to on_message or built-in handlers."""
        self._log.debug("dispatch loop started")
        try:
            async for received in self._client.recv_stream():
                if not self._running:
                    break
                await self._route(received)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("dispatch loop crashed", error=str(exc))
            raise

    async def _route(self, received: ReceivedMessage) -> None:
        msg = received.message
        mtype = msg.message_type

        # Built-in PING → auto PONG
        if mtype == MessageType.PING:
            await self._handle_ping(received)
            return

        # Delegate everything else to the subclass
        try:
            await self.on_message(received)
        except Exception as exc:
            self._log.error(
                "on_message raised",
                msg_type=mtype,
                error=str(exc),
            )

    async def _handle_ping(self, received: ReceivedMessage) -> None:
        ping_msg = received.message
        t0 = time.monotonic()
        pong = make_pong(
            ping=ping_msg,
            sender_role=self.role,
            sender_pubkey=self.pubkey,
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
        )
        dest = received.from_pubkey
        await self._client.send(dest, pong)
        self._log.debug(
            "pong sent",
            to=dest[:16] + "…",
            nonce=ping_msg.payload.get("nonce", "")[:8],
        )

    # ── subclass contract ─────────────────────────────────────────────────────

    @abstractmethod
    async def on_message(self, received: ReceivedMessage) -> None:
        """
        Handle an inbound SwarmMessage.  PING is handled by the base class.
        Subclasses handle MARKET_SIGNAL, TRADE_DECISION, EXECUTION_RESULT, etc.
        """
        ...