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
from core.storage.client import ZeroGClient
from core.storage.agent_memory import AgentMemory, make_agent_memory
from core.storage.models import AgentStatus, LogEventType

log = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """
    Base class for all SwarmFi agents.
    Stage 2: now carries persistent memory via 0G Storage.
    """

    def __init__(
        self,
        role:       AgentRole,
        registry:   AgentRegistry,
        zg_client:  ZeroGClient | None = None,
    ) -> None:
        self.role      = role
        self.registry  = registry
        self._zg       = zg_client
        self._log      = log.bind(agent=role.value)

        api_url = registry.api_url_for(role)
        self._client = AXLClient(api_url, agent_name=role.value)

        self._pubkey: str = ""
        self._running        = False
        self._dispatch_task: asyncio.Task[None] | None = None

        # Populated in start() if zg_client provided
        self.memory: AgentMemory | None = None

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def pubkey(self) -> str:
        if not self._pubkey:
            raise RuntimeError("Agent not started — pubkey not yet known")
        return self._pubkey

    async def start(self) -> None:
        await self._client.__aenter__()
        info = await self._client.topology()
        self._pubkey  = info.public_key
        self._running = True

        # Wire up 0G memory if client provided
        if self._zg is not None:
            self.memory = make_agent_memory(self.role.value, self._zg)
            await self.memory.update_status(AgentStatus.IDLE)
            await self.memory.log_event(
                LogEventType.AGENT_STARTED,
                data={"pubkey": self._pubkey[:16] + "…"},
            )
            self._log.info("memory: 0G Storage connected")
        else:
            self._log.info("memory: no 0G client — running stateless")

        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name=f"{self.role.value}-dispatch"
        )
        self._log.info("agent started", pubkey=self._pubkey[:16] + "…")

    async def stop(self) -> None:
        self._running = False

        if self.memory is not None:
            try:
                await self.memory.update_status(AgentStatus.IDLE)
                await self.memory.log_event(LogEventType.AGENT_STOPPED)
            except Exception as exc:
                self._log.warning("memory: failed to write stop event", error=str(exc))

        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        await self._client.__aexit__(None, None, None)
        self._log.info("agent stopped")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            self._log.info("shutdown signal received")
            loop.create_task(self.stop())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown)

        await self.start()
        while self._running:
            await asyncio.sleep(0.1)

    async def send(self, dest_role: AgentRole, message: SwarmMessage) -> None:
        dest_pubkey = self.registry.pubkey_for(dest_role)
        await self._client.send(dest_pubkey, message)

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
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
        msg   = received.message
        mtype = msg.message_type

        if mtype == MessageType.PING:
            await self._handle_ping(received)
            return

        try:
            await self.on_message(received)
        except Exception as exc:
            self._log.error("on_message raised", msg_type=mtype, error=str(exc))

    async def _handle_ping(self, received: ReceivedMessage) -> None:
        ping_msg = received.message
        t0   = time.monotonic()
        pong = make_pong(
            ping=ping_msg,
            sender_role=self.role,
            sender_pubkey=self.pubkey,
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
        )
        await self._client.send(received.from_pubkey, pong)
        self._log.debug(
            "pong sent",
            to=received.from_pubkey[:16] + "…",
            nonce=ping_msg.payload.get("nonce", "")[:8],
        )

    @abstractmethod
    async def on_message(self, received: ReceivedMessage) -> None: ...