"""
dashboard/server.py — Production SwarmFi Dashboard Server

Endpoints:
  GET  /              → dashboard HTML
  GET  /api/state     → live swarm state from 0G Storage
  GET  /api/log       → recent log entries from 0G Storage
  POST /api/chat      → AI chat via 0G Compute (user queries + strategy config)
  POST /api/signal    → inject a manual trade signal into the swarm
  GET  /api/pairs     → supported token pairs
  GET  /api/config    → current swarm config (risk threshold, amount, etc.)
  POST /api/config    → update swarm config live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Shared mutable swarm config ───────────────────────────────────────────────
_swarm_config: dict[str, Any] = {
    "risk_threshold":   float(os.getenv("RISK_THRESHOLD", "7.0")),
    "scan_interval_s":  int(os.getenv("SCAN_INTERVAL", "60")),
    "default_pair": {
        "token_in":      "0x0000000000000000000000000000000000000000",
        "token_out":     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "token_in_sym":  "ETH",
        "token_out_sym": "USDC",
        "chain_id":      8453,
    },
    "amount_in_wei": 50_000_000_000_000_000,  # 0.05 ETH
    "auto_trade":    False,   # safety: manual only by default
}

# Base (chain 8453) token addresses
_KNOWN_PAIRS = [
    {"label": "ETH → USDC",  "token_in": "0x0000000000000000000000000000000000000000", "token_out": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "in_sym": "ETH",  "out_sym": "USDC",  "chain_id": 8453},
    {"label": "ETH → USDT",  "token_in": "0x0000000000000000000000000000000000000000", "token_out": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "in_sym": "ETH",  "out_sym": "USDT",  "chain_id": 8453},
    {"label": "USDC → ETH",  "token_in": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "token_out": "0x0000000000000000000000000000000000000000", "in_sym": "USDC", "out_sym": "ETH",   "chain_id": 8453},
    {"label": "WETH → USDC", "token_in": "0x4200000000000000000000000000000000000006", "token_out": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "in_sym": "WETH", "out_sym": "USDC",  "chain_id": 8453},
]

# ── Storage helpers ────────────────────────────────────────────────────────────

async def _get_zg_state() -> dict:
    try:
        from core.storage.client import ZeroGClient
        from core.storage.kv import SwarmKV
        from core.storage.models import KVKey
        async with ZeroGClient.from_env() as zg:
            kv  = SwarmKV(zg)
            raw = await kv.get(KVKey.SWARM_STATE)
            if raw:
                return json.loads(raw.decode())
    except Exception as e:
        pass
    return {}

async def _get_zg_log(limit: int = 30) -> list:
    try:
        from core.storage.client import ZeroGClient
        from core.storage.kv import SwarmKV
        from core.storage.log import SwarmLog
        async with ZeroGClient.from_env() as zg:
            kv   = SwarmKV(zg)
            slog = SwarmLog(zg, kv)
            entries = await slog.recent(limit=limit)
            return [
                {
                    "event_type": e.event_type.value,
                    "agent_role": e.agent_role,
                    "timestamp":  e.timestamp.isoformat(),
                    "data":       e.data,
                }
                for e in entries
            ]
    except Exception:
        return []

# ── Chat with 0G Compute ──────────────────────────────────────────────────────

_CHAT_SYSTEM = """You are SwarmFi's AI trading assistant, running on the 0G Compute Network.
You help users configure and understand their autonomous DeFi trading swarm.

Current swarm capabilities:
- Monitors ETH/USDC price signals on Base (chain 8453)
- Uses AI risk scoring to approve or reject trades
- Executes swaps via Uniswap Trading API with KeeperHub guaranteed delivery
- Stores all decisions permanently on 0G decentralized storage
- P2P encrypted agent communication via Gensyn AXL

You can help with:
- Explaining trading strategies and how to tune the risk threshold
- Analysing recent swarm decisions from the history log
- Suggesting which token pairs to trade
- Explaining how each component works

Always be specific, concise, and technically accurate. When discussing trades, mention risk scores."""

async def _ai_chat(messages: list[dict]) -> str:
    try:
        from core.compute.client import ZeroGComputeClient
        async with ZeroGComputeClient.from_env() as client:
            full_msgs = [{"role": "system", "content": _CHAT_SYSTEM}] + messages
            return await client.chat(full_msgs, max_tokens=800)
    except Exception as exc:
        return f"AI unavailable: {exc}"

# ── ENS resolution helper ─────────────────────────────────────────────────────

async def _resolve_ens(name: str) -> str | None:
    """Resolve any ENS name to an address. Used when sending profits to ENS names."""
    if name.startswith("0x") and len(name) == 42:
        return name   # already an address
    try:
        import httpx
        # Use public ENS resolution via Cloudflare/ENS API
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.ensideas.com/ens/resolve/{name}"
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("address")
    except Exception:
        pass
    # Fallback: try web3/ens if installed
    try:
        from ens import ENS
        from web3 import Web3
        w3  = Web3(Web3.HTTPProvider("https://eth.llamarpc.com"))
        ns  = ENS.from_web3(w3)
        return ns.address(name)
    except Exception:
        return None

# ── FastAPI app ───────────────────────────────────────────────────────────────

def main() -> None:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel
        import uvicorn
    except ImportError:
        print("pip install fastapi uvicorn")
        sys.exit(1)

    app  = FastAPI(title="SwarmFi Dashboard API")
    root = Path(__file__).parent

    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    # ── Static ────────────────────────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(root / "index.html")

    # ── Live agent state ──────────────────────────────────────────────────────
    @app.get("/api/state")
    async def state():
        data = await _get_zg_state()
        if not data:
            # Return truthful empty state — no demo faking
            return JSONResponse({
                "version": 0,
                "updated_at": None,
                "agents": {},
                "config": _swarm_config,
            })
        data["config"] = _swarm_config
        return JSONResponse(data)

    @app.get("/api/log")
    async def log_entries():
        return JSONResponse(await _get_zg_log(limit=50))

    # ── AI Chat ───────────────────────────────────────────────────────────────
    class ChatRequest(BaseModel):
        messages: list[dict]

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        reply = await _ai_chat(req.messages)
        return JSONResponse({"reply": reply})

    # ── Inject trade signal ───────────────────────────────────────────────────
    class SignalRequest(BaseModel):
        token_in:  str
        token_out: str
        chain_id:  int = 8453
        signal:    str = "strong"
        reason:    str = "Manual signal from dashboard"
        amount_wei: int = 50_000_000_000_000_000

    @app.post("/api/signal")
    async def inject_signal(req: SignalRequest):
        """Inject a trade signal into the swarm — runs a real trade cycle."""
        try:
            import httpx
            # Fetch live price for the signal
            price = 0.0
            try:
                async with httpx.AsyncClient(timeout=5) as hclient:
                    r = await hclient.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": "ethereum", "vs_currencies": "usd"}
                    )
                    price = r.json().get("ethereum", {}).get("usd", 0.0)
            except Exception:
                pass

            signal = {
                "token_in":      req.token_in,
                "token_out":     req.token_out,
                "chain_id":      req.chain_id,
                "price_usd":     price,
                "signal":        req.signal,
                "reason":        req.reason,
                "amount_in_wei": req.amount_wei,
            }

            # Run the full trade cycle in background
            asyncio.create_task(_run_trade_cycle(signal))
            return JSONResponse({"status": "signal_injected", "signal": signal})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── ENS resolution ────────────────────────────────────────────────────────
    @app.get("/api/ens/resolve/{name}")
    async def resolve_ens(name: str):
        address = await _resolve_ens(name)
        if address:
            return JSONResponse({"name": name, "address": address, "resolved": True})
        return JSONResponse({"name": name, "address": None, "resolved": False})

    # ── Transfer to ENS name ──────────────────────────────────────────────────
    class TransferRequest(BaseModel):
        recipient: str   # ENS name OR 0x address
        amount:    str   # human-readable e.g. "0.01"
        network:   str   = "base"

    @app.post("/api/transfer")
    async def transfer_to_ens(req: TransferRequest):
        """Resolve ENS name and execute transfer via KeeperHub."""
        address = await _resolve_ens(req.recipient)
        if not address:
            raise HTTPException(400, f"Could not resolve {req.recipient}")

        try:
            from core.keeperhub.client import KeeperHubClient
            from core.keeperhub.models import KHTransferRequest, KHNetwork
            async with KeeperHubClient.from_env() as kh:
                kh_req = KHTransferRequest(
                    network=KHNetwork(req.network),
                    recipientAddress=address,
                    amount=req.amount,
                )
                result = await kh.execute_transfer(kh_req)
                return JSONResponse({
                    "execution_id": result.execution_id,
                    "resolved_address": address,
                    "recipient": req.recipient,
                    "amount": req.amount,
                })
        except Exception as exc:
            raise HTTPException(500, str(exc))

    # ── Config ────────────────────────────────────────────────────────────────
    @app.get("/api/config")
    async def get_config():
        return JSONResponse(_swarm_config)

    @app.post("/api/config")
    async def update_config(updates: dict):
        for k, v in updates.items():
            if k in _swarm_config:
                _swarm_config[k] = v
        return JSONResponse(_swarm_config)

    @app.get("/api/pairs")
    async def get_pairs():
        return JSONResponse(_KNOWN_PAIRS)

    # ── App ───────────────────────────────────────────────────────────────────
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    print(f"\n  SwarmFi Dashboard → http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


async def _run_trade_cycle(signal: dict) -> None:
    """Background task: run full researcher→risk→executor cycle with real APIs."""
    try:
        from core.storage.client import ZeroGClient
        from core.storage.agent_memory import make_shared_memory_set
        from core.compute.client import ZeroGComputeClient
        from core.compute.risk_scorer import RiskScorer
        from core.keeperhub.client import KeeperHubClient
        from core.keeperhub.executor import KeeperHubSwapExecutor
        from core.uniswap.client import UniswapClient
        from core.storage.models import AgentStatus, LogEventType

        zg      = ZeroGClient.from_env()
        compute = ZeroGComputeClient.from_env()
        uniswap = UniswapClient.from_env()
        kh      = KeeperHubClient.from_env()
        wallet  = os.getenv("WALLET_ADDRESS", "")

        async with zg, compute, uniswap, kh:
            memories = make_shared_memory_set(["researcher", "risk", "executor"], zg)

            # Researcher
            await memories["researcher"].update_status(AgentStatus.SCANNING, last_signal=signal)
            await memories["researcher"].log_event(LogEventType.MARKET_SIGNAL, data=signal)

            # Risk — real 0G Compute AI scoring
            await memories["risk"].update_status(AgentStatus.DECIDING)
            scorer   = RiskScorer(compute)
            decision = await scorer.score(signal=signal, sender_pubkey="0"*64, our_pubkey="0"*64)
            payload  = decision.payload

            await memories["risk"].log_event(LogEventType.RISK_DECISION, data=payload)
            await memories["risk"].update_status(AgentStatus.IDLE, last_risk_score=payload.get("risk_score"))

            if payload.get("action") == "hold":
                await memories["executor"].log_event(LogEventType.TRADE_FAILED,
                    data={"action": "HOLD", "reason": payload.get("rejection_reason")})
                return

            # Executor — real Uniswap + KeeperHub
            await memories["executor"].update_status(AgentStatus.EXECUTING)
            executor = KeeperHubSwapExecutor(uniswap=uniswap, keeperhub=kh, wallet_address=wallet, zg_client=zg)
            result   = await executor.execute_swap(
                token_in=payload.get("token_in"),
                token_out=payload.get("token_out"),
                amount_in_wei=str(payload.get("amount_in_wei", 50_000_000_000_000_000)),
                chain_id=payload.get("chain_id", 8453),
                wallet_address=wallet,
            )

            if result.succeeded or result.status.value == "submitted":
                await memories["executor"].update_status(AgentStatus.IDLE, last_tx_hash=result.tx_hash)
                await memories["executor"].log_event(LogEventType.TRADE_EXECUTED, data=result.to_log_data())
            else:
                await memories["executor"].update_status(AgentStatus.ERROR, error_message=result.error)
                await memories["executor"].log_event(LogEventType.TRADE_FAILED, data=result.to_log_data())

    except Exception as exc:
        import structlog
        structlog.get_logger("dashboard").error("trade cycle failed", error=str(exc))


if __name__ == "__main__":
    main()