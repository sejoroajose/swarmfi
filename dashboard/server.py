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

# The demo process writes ./logs/swarmfi-state.json after each cycle.
# This is the dashboard's primary source of truth (the dashboard is a
# separate Python process, so it can't share the demo's in-memory state).
_STATE_FILE = Path(__file__).parent.parent / "logs" / "swarmfi-state.json"


def _read_state_file() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


async def _get_zg_state() -> dict:
    """
    Return the latest swarm state for the dashboard.
    Source of truth: the demo's ./logs/swarmfi-state.json sidecar.
    """
    view = _read_state_file()
    if not view:
        return {}

    # Build a minimal state shape the frontend already understands.
    results = view.get("results", []) or []
    last = results[-1] if results else {}
    last = last if isinstance(last, dict) else {}
    agents: dict[str, dict] = {
        "researcher": {
            "status":      "IDLE",
            "last_signal": last.get("signal", {}),
        },
        "risk": {
            "status":          "IDLE",
            "last_risk_score": last.get("risk"),
            "last_action":     last.get("action"),
            "last_confidence": last.get("confidence"),
        },
        "executor": {
            "status":       "IDLE",
            "last_tx_hash": last.get("tx"),
            "last_routing": last.get("routing"),
        },
    }
    in_progress = view.get("in_progress")
    if isinstance(in_progress, dict):
        stage = in_progress.get("stage")
        sig   = in_progress.get("signal", {})
        if stage in ("scanning", "deciding", "executing", "committing"):
            agents["researcher"]["last_signal"] = sig or agents["researcher"]["last_signal"]
        if stage == "scanning":
            agents["researcher"]["status"] = "SCANNING"
        elif stage == "deciding":
            agents["researcher"]["status"] = "IDLE"
            agents["risk"]["status"]       = "DECIDING"
            if in_progress.get("risk") is not None:
                agents["risk"]["last_risk_score"] = in_progress["risk"]
                agents["risk"]["last_action"]     = in_progress.get("action")
                agents["risk"]["last_confidence"] = in_progress.get("confidence")
        elif stage == "executing":
            agents["risk"]["last_risk_score"] = in_progress.get("risk")
            agents["risk"]["last_action"]     = in_progress.get("action")
            agents["executor"]["status"]      = "EXECUTING"
        elif stage == "committing":
            agents["executor"]["status"]      = "EXECUTING"
            if in_progress.get("tx"):
                agents["executor"]["last_tx_hash"] = in_progress["tx"]

    return {
        "version":       view.get("cycles", 0),
        "updated_at":    view.get("updated_at"),
        "snapshot_root": view.get("snapshot_root"),
        "snapshot_tx":   view.get("snapshot_tx"),
        "in_progress":   in_progress,
        "agents":        agents,
    }


async def _get_zg_log(limit: int = 30) -> list:
    """
    Reconstruct a flat event log from the demo's recent results.
    Each cycle produces: MARKET_SIGNAL → RISK_DECISION → TRADE_*.
    """
    view = _read_state_file()
    results = view.get("results", []) or []
    out: list[dict] = []
    for r in results:
        cyc = r.get("cycle", 0)
        # Market signal
        out.append({
            "event_type": "MARKET_SIGNAL",
            "agent_role": "researcher",
            "timestamp":  view.get("updated_at"),
            "data":       {"cycle": cyc},
        })
        # Risk decision
        out.append({
            "event_type": "RISK_DECISION",
            "agent_role": "risk",
            "timestamp":  view.get("updated_at"),
            "data": {
                "cycle":     cyc,
                "risk_score": r.get("risk"),
                "action":    r.get("action"),
            },
        })
        # Trade outcome
        if r.get("tx"):
            out.append({
                "event_type": "TRADE_EXECUTED",
                "agent_role": "executor",
                "timestamp":  view.get("updated_at"),
                "data":       {"cycle": cyc, "tx_hash": r.get("tx")},
            })
        elif r.get("action") == "hold":
            pass  # already captured in RISK_DECISION
        elif r.get("error"):
            out.append({
                "event_type": "TRADE_FAILED",
                "agent_role": "executor",
                "timestamp":  view.get("updated_at"),
                "data":       {"cycle": cyc, "error": r.get("error")},
            })
    return out[-limit:]

# ── Chat with 0G Compute ──────────────────────────────────────────────────────

_CHAT_SYSTEM = """You are SwarmFi AI — the in-app assistant for the SwarmFi
autonomous DeFi swarm running on 0G Compute. You speak with first-person
authority about THIS specific swarm. You are NOT a generic crypto advisor and
you NEVER invent generic trading-book content (no RSI tutorials, no MACD
explainers, no "buy low sell high" filler).

The swarm is exactly three agents on 0G + Base:

  researcher.swarmfi.eth   — pulls ETH/USDC (and other Base) price signals
                             and writes them to 0G Storage as the shared
                             market-signal record.
  risk.swarmfi.eth         — uses 0G Compute (sealed inference, models like
                             qwen3-plus / GLM-FP8) to score every signal
                             0–10. Anything above the risk threshold is
                             auto-rejected — the agent returns HOLD with a
                             one-line reason.
  executor.swarmfi.eth     — fetches a swap quote from the Uniswap Trading
                             API and submits the calldata to KeeperHub,
                             which guarantees broadcast (retry, gas mgmt,
                             private routing). Returns the tx hash.

The three agents communicate peer-to-peer over Gensyn AXL (encrypted, no
central broker). All status, signals, decisions, and tx hashes are
persisted on 0G Storage as one snapshot per cycle — the snapshot root is
visible in the dashboard summary.

How to answer:
  - Always ground responses in the SWARM CONTEXT and LIVE MARKET SCAN
    blocks below. Reference real numbers from them — current prices,
    24h momentum, composite edge scores, risk scores, root hashes,
    tx hashes.
  - If the user asks "what is X price" or "what's the best opportunity",
    answer from the LIVE MARKET SCAN block — that's regenerated on every
    request, so it IS real-time. Never claim 'I can't see live prices'.
  - The scan covers ETH, WETH, USDC, USDT, and cbBTC (BTC-pegged) on
    Base. Yes, that means you DO have live BTC pricing via cbBTC.
  - If a user asks for a token NOT in the scan (e.g. SOL, AVAX), say so
    plainly — the swarm only scans Base bluechips by design.
  - When explaining a sponsor (0G / Uniswap / KeeperHub / Gensyn / ENS),
    describe the role it plays in THIS swarm, not the product in general.
  - Be terse. 4–8 sentences usually. No bullet-list essays unless asked.
  - Never use phrases like "as an AI" or "I cannot provide financial
    advice". You are an embedded operator, not a chatbot."""

def _short_hash(s: str | None, head: int = 10, tail: int = 6) -> str:
    if not s:
        return "—"
    s = str(s)
    if len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"

async def _build_swarm_context() -> str:
    """Render a compact live-state block to inject into the system prompt."""
    try:
        state = await _get_zg_state()
        log   = await _get_zg_log(limit=8)
    except Exception:
        state, log = {}, []

    # Live multi-pair scanner — this is what gives the AI access to fresh
    # prices, 24h momentum, and the current best opportunity. Without this
    # the chat agent only sees the *last completed cycle* which can be many
    # minutes stale and reads as broken to the user.
    scan_block: list[str] = []
    try:
        from core.scanner import scan_pairs
        scan = await scan_pairs()
        if scan and scan.ranked:
            scan_block.append("LIVE MARKET SCAN (regenerated this request, all Base bluechips):")
            for s in scan.ranked:
                star = " ★ best" if s is scan.best else ""
                scan_block.append(
                    f"  - {s.pair.label:<14}"
                    f"price=${s.price_usd:>10,.2f}  "
                    f"24h={s.momentum_24h:+.2f}%  "
                    f"edge={s.composite:.2f}  "
                    f"signal={s.signal}{star}"
                )
    except Exception:
        pass

    cfg = _swarm_config
    pair = cfg.get("default_pair", {})
    lines = ["SWARM CONTEXT (live, regenerated each request):"]
    lines.append(
        f"  config: pair={pair.get('token_in_sym','?')}→{pair.get('token_out_sym','?')} "
        f"chain={pair.get('chain_id','?')} "
        f"risk_threshold={cfg.get('risk_threshold','?')} "
        f"amount_in_wei={cfg.get('amount_in_wei','?')} "
        f"auto_trade={cfg.get('auto_trade', False)}"
    )
    lines.append(
        "  note: 'auto_trade=False' means the swarm runs cycles ONLY on demand "
        "(via /api/signal or the 'Run a cycle' button). Each manual cycle still "
        "executes a real trade decision via the executor agent."
    )

    snap = (state or {}).get("snapshot_root") or os.getenv("SWARMFI_SNAPSHOT_ROOT", "").strip()
    if snap:
        lines.append(f"  latest_0g_snapshot: {_short_hash(snap)}")

    agents = (state or {}).get("agents") or {}
    if agents:
        lines.append(f"  agents_seen: {len(agents)}  state_version: {state.get('version', 0)}")
        for role, a in agents.items():
            lines.append(
                f"    - {role:10s} status={a.get('status','?'):10s} "
                f"risk={a.get('last_risk_score','—')} "
                f"tx={_short_hash(a.get('last_tx_hash'))}"
            )
    else:
        lines.append("  agents_seen: 0  (no on-chain snapshot loaded yet)")

    if scan_block:
        lines.append("")
        lines.extend(scan_block)
        lines.append("")

    if log:
        lines.append(f"  recent_events ({len(log)}):")
        for e in log[-8:]:
            data = e.get("data") or {}
            extras = []
            if "risk_score" in data:    extras.append(f"risk={data['risk_score']}")
            if "action"     in data:    extras.append(f"action={data['action']}")
            if "price_usd"  in data:    extras.append(f"price=${data['price_usd']}")
            if "tx_hash"    in data:    extras.append(f"tx={_short_hash(data['tx_hash'])}")
            extra = (" " + " ".join(extras)) if extras else ""
            lines.append(f"    - [{e.get('agent_role','?')}] {e.get('event_type','?')}{extra}")
    else:
        lines.append("  recent_events: 0  (run a cycle to populate)")
    return "\n".join(lines)

async def _ai_chat(messages: list[dict]) -> str:
    try:
        from core.compute.client import ZeroGComputeClient
        context = await _build_swarm_context()
        system  = _CHAT_SYSTEM + "\n\n" + context
        async with ZeroGComputeClient.from_env() as client:
            full_msgs = [{"role": "system", "content": system}] + messages
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
    # Permissive request handling: the frontend sends {messages:[{role,content},…]},
    # but we also accept {message:"…"}, a bare string, or {prompt:"…"} so an
    # accidental 422 never blocks the demo. Bad payloads return 200 + an
    # explanatory reply instead of a confusing validation error.
    from fastapi import Body

    def _normalise_chat_payload(body: Any) -> list[dict] | None:
        if body is None:
            return None
        if isinstance(body, str) and body.strip():
            return [{"role": "user", "content": body.strip()}]
        if isinstance(body, dict):
            if isinstance(body.get("messages"), list):
                msgs: list[dict] = []
                for m in body["messages"]:
                    if isinstance(m, dict) and m.get("content"):
                        msgs.append({
                            "role":    str(m.get("role", "user")),
                            "content": str(m["content"]),
                        })
                    elif isinstance(m, str):
                        msgs.append({"role": "user", "content": m})
                return msgs or None
            for k in ("message", "prompt", "text", "input"):
                v = body.get(k)
                if isinstance(v, str) and v.strip():
                    return [{"role": "user", "content": v.strip()}]
        return None

    @app.post("/api/chat")
    async def chat(payload: Any = Body(default=None)):
        msgs = _normalise_chat_payload(payload)
        if not msgs:
            return JSONResponse(
                {"reply": "I didn't receive any message. Try typing something into the chat box."},
                status_code=200,
            )
        reply = await _ai_chat(msgs)
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
    async def inject_signal(payload: Any = Body(default=None)):
        """Inject a trade signal into the swarm — runs a real trade cycle."""
        try:
            body = payload if isinstance(payload, dict) else {}

            token_in   = str(body.get("token_in")  or "0x0000000000000000000000000000000000000000")
            token_out  = str(body.get("token_out") or "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
            chain_id   = int(body.get("chain_id") or 8453)
            sig_str    = str(body.get("signal")    or "strong")
            reason     = str(body.get("reason")    or "Manual signal from dashboard")
            amount_wei = int(body.get("amount_wei") or 50_000_000_000_000_000)

            import httpx
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
                "token_in":      token_in,
                "token_out":     token_out,
                "chain_id":      chain_id,
                "price_usd":     price,
                "signal":        sig_str,
                "reason":        reason,
                "amount_in_wei": amount_wei,
            }
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
    async def transfer_to_ens(payload: Any = Body(default=None)):
        """Resolve ENS name and execute transfer via KeeperHub."""
        body = payload if isinstance(payload, dict) else {}
        recipient = str(body.get("recipient") or "").strip()
        amount    = str(body.get("amount")    or "").strip()
        network   = str(body.get("network")   or "base").strip()
        if not recipient or not amount:
            raise HTTPException(400, "recipient and amount required")

        address = await _resolve_ens(recipient)
        if not address:
            raise HTTPException(400, f"Could not resolve {recipient}")

        try:
            from core.keeperhub.client import KeeperHubClient
            from core.keeperhub.models import KHTransferRequest, KHNetwork
            async with KeeperHubClient.from_env() as kh:
                kh_req = KHTransferRequest(
                    network=KHNetwork(network),
                    recipientAddress=address,
                    amount=amount,
                )
                result = await kh.execute_transfer(kh_req)
                return JSONResponse({
                    "execution_id": result.execution_id,
                    "resolved_address": address,
                    "recipient": recipient,
                    "amount": amount,
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

    # ── Live AXL message stream (researcher ↔ risk ↔ executor) ──────────────
    @app.get("/api/axl")
    async def get_axl_events():
        """Recent inter-node AXL /send events. Empty until a cycle has run."""
        from core.axl_bus import recent_events
        return JSONResponse({"events": recent_events(limit=20)})

    # ── ENS-backed agent identity profiles ──────────────────────────────────
    #
    # Returns the live ENS profile for each swarm role — name, address,
    # AXL pubkey, and SwarmFi text records (status, last decision, tx,
    # snapshot root). The dashboard reads agent metadata exclusively from
    # this endpoint; nothing about agent identity is hardcoded client-side.
    @app.get("/api/agents")
    async def get_agents():
        from core.ens.resolver import AgentIdentity
        identity = AgentIdentity.from_env()
        roles = ["researcher", "risk", "executor"]
        profiles = []
        for role in roles:
            try:
                profiles.append(await identity.get_profile(role))
            except Exception as exc:
                profiles.append({
                    "name":    f"{role}.swarmfi.eth",
                    "role":    role,
                    "error":   str(exc)[:120],
                })
        return JSONResponse({"agents": profiles})

    # ── Multi-pair scanner ────────────────────────────────────────────────────
    @app.get("/api/scan")
    async def get_scan():
        """Return the live edge-profile scan across all bluechip pairs."""
        from core.scanner import scan_pairs
        try:
            result = await scan_pairs()
            return JSONResponse(result.to_dict())
        except Exception as exc:
            return JSONResponse({"error": str(exc), "ranked": []}, status_code=200)

    # ── Combined dashboard endpoint ──────────────────────────────────────────
    #
    # Polling six independent endpoints every 1.5 s used to fire ~4 req/s
    # at the browser, eating connection-slot budget and starving the chat
    # endpoint when it was waiting on the LLM. This one endpoint runs all
    # sub-fetches concurrently in-process via asyncio.gather and returns
    # everything in one payload.
    @app.get("/api/dashboard")
    async def get_dashboard():
        from core.scanner    import scan_pairs, _price_cache  # type: ignore[attr-defined]
        from core.pnl        import compute_pnl
        from core.axl_bus    import recent_events
        from core.ens.resolver import AgentIdentity

        async def _state():
            return await _get_zg_state()

        async def _log():
            return await _get_zg_log(limit=30)

        async def _scan():
            try:
                return (await scan_pairs()).to_dict()
            except Exception as exc:
                return {"error": str(exc), "ranked": []}

        async def _agents():
            identity = AgentIdentity.from_env()
            profiles: list[dict] = []
            for role in ("researcher", "risk", "executor"):
                try:
                    profiles.append(
                        await asyncio.wait_for(identity.get_profile(role), timeout=1.5)
                    )
                except Exception:
                    profiles.append({
                        "name": f"{role}.swarmfi.eth",
                        "role": role,
                    })
            view = _read_state_file()
            sidecar = view.get("agent_profiles") or []
            if sidecar:
                by_role = {p.get("role"): p for p in sidecar}
                for p in profiles:
                    fb = by_role.get(p.get("role")) or {}
                    for k in ("status", "last", "tx", "snapshot", "axl_pubkey"):
                        if not p.get(k) and fb.get(k):
                            p[k] = fb[k]
            return {"agents": profiles}

        # AXL + PnL are sync work over already-cached data — no point gathering
        def _axl():
            events = recent_events(limit=20)
            if not events:
                view = _read_state_file()
                events = view.get("axl_events") or []
            return {"events": events}

        def _pnl():
            try:
                view = _read_state_file()
                results = list(view.get("results") or [])
                sym_for = {"ethereum": "ETH", "bitcoin": "cbBTC"}
                prices: dict[str, float] = {}
                for cg_id, info in (_price_cache or {}).items():
                    sym = sym_for.get(cg_id)
                    if sym and isinstance(info, dict) and info.get("usd"):
                        prices[sym] = float(info["usd"])
                if "ETH" in prices and "WETH" not in prices:
                    prices["WETH"] = prices["ETH"]
                commit = float(os.getenv("SWARMFI_COMMITMENT_ETH") or "0.0001")
                return compute_pnl(results, prices, commitment_eth=commit).to_dict()
            except Exception as exc:
                return {"error": str(exc)}

        # Run the truly-async ones in parallel; sync ones complete instantly
        state, log, scan, agents = await asyncio.gather(
            _state(), _log(), _scan(), _agents(),
            return_exceptions=False,
        )
        return JSONResponse({
            "state":  state,
            "log":    log,
            "scan":   scan,
            "agents": agents.get("agents") if isinstance(agents, dict) else [],
            "axl":    _axl(),
            "pnl":    _pnl(),
        })

    # ── App ───────────────────────────────────────────────────────────────────
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    print(f"\n  SwarmFi Dashboard → http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def _publish_state_view(
    snapshot_root: str | None,
    result: dict,
    snapshot_tx: str | None = None,
) -> None:
    """Append a result to ./logs/swarmfi-state.json so the dashboard sees it."""
    try:
        from datetime import datetime, timezone
        view = _read_state_file() or {"results": []}
        results = list(view.get("results") or [])
        results.append(result)
        out = {
            "updated_at":    datetime.now(tz=timezone.utc).isoformat(),
            "snapshot_root": snapshot_root or view.get("snapshot_root"),
            "snapshot_tx":   snapshot_tx   or view.get("snapshot_tx"),
            "cycles":        len(results),
            "results":       results[-20:],
            "pair":          result.get("signal") or view.get("pair") or {},
            "in_progress":   None,
        }
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(out, indent=2))
    except Exception:
        pass


def _publish_partial(stage: str, partial: dict) -> None:
    """
    Stream a step-level update to the sidecar so the dashboard reflects
    progress LIVE during a cycle. `stage` is one of:
      'scanning' | 'deciding' | 'executing' | 'committing'
    """
    try:
        from datetime import datetime, timezone
        view = _read_state_file() or {}
        view["updated_at"]  = datetime.now(tz=timezone.utc).isoformat()
        view["in_progress"] = {"stage": stage, **partial}
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(view, indent=2))
    except Exception:
        pass


async def _run_trade_cycle(signal: dict) -> None:
    """Background task: run full researcher→risk→executor cycle with real APIs.
    Commits a 0G snapshot at the end and updates the dashboard state file."""
    import structlog
    log = structlog.get_logger("dashboard")
    try:
        from core.storage.client import ZeroGClient
        from core.storage.agent_memory import make_shared_memory_set
        from core.compute.client import ZeroGComputeClient
        from core.compute.risk_scorer import RiskScorer
        from core.keeperhub.client import KeeperHubClient
        from core.keeperhub.executor import KeeperHubSwapExecutor
        from core.uniswap.client import UniswapClient
        from core.storage.models import AgentStatus, LogEventType
        from core import axl_bus
        from core.ens.resolver import AgentIdentity
        identity = AgentIdentity.from_env()

        zg      = ZeroGClient.from_env()
        compute = ZeroGComputeClient.from_env()
        uniswap = UniswapClient.from_env()
        kh      = KeeperHubClient.from_env()
        wallet  = os.getenv("WALLET_ADDRESS", "")

        # Track outcome for the state file
        result_view: dict[str, Any] = {
            "cycle":  (_read_state_file().get("cycles") or 0) + 1,
            "signal": signal,
        }

        async with zg, compute, uniswap, kh:
            memories = make_shared_memory_set(["researcher", "risk", "executor"], zg)

            # ── Researcher ──────────────────────────────────────────────────
            _publish_partial("scanning", {"signal": signal})
            await memories["researcher"].update_status(AgentStatus.SCANNING, last_signal=signal)
            await memories["researcher"].log_event(LogEventType.MARKET_SIGNAL, data=signal)

            # ENS: write the researcher's latest market scan into its text records.
            # Every read of /api/agents now sees this via ENS resolution.
            await identity.update_text("researcher", "swarmfi.role",   "market_scanner")
            await identity.update_text("researcher", "swarmfi.status", "scanning")
            await identity.update_text(
                "researcher", "swarmfi.last",
                f"{signal.get('token_in_sym','?')}→{signal.get('token_out_sym','?')} "
                f"@ ${float(signal.get('price_usd',0)):,.2f} · {signal.get('signal','?')}"
            )

            # AXL: researcher → risk (real /send between separate AXL nodes)
            await axl_bus.announce_market_signal(signal)

            # ── Risk ────────────────────────────────────────────────────────
            _publish_partial("deciding", {"signal": signal})
            await memories["risk"].update_status(AgentStatus.DECIDING)
            scorer   = RiskScorer(compute)
            decision = await scorer.score(signal=signal, sender_pubkey="0"*64, our_pubkey="0"*64)
            payload  = decision.payload

            await memories["risk"].log_event(LogEventType.RISK_DECISION, data=payload)
            await memories["risk"].update_status(AgentStatus.IDLE, last_risk_score=payload.get("risk_score"))

            result_view["risk"]       = payload.get("risk_score")
            result_view["action"]     = payload.get("action")
            result_view["confidence"] = payload.get("confidence")
            result_view["reasoning"]  = payload.get("reasoning")

            _publish_partial("deciding", {
                "signal": signal, "risk": payload.get("risk_score"),
                "action": payload.get("action"), "confidence": payload.get("confidence"),
            })

            # ENS: write the risk agent's decision summary
            await identity.update_text("risk", "swarmfi.role",   "ai_risk_scoring")
            await identity.update_text("risk", "swarmfi.status", "idle")
            await identity.update_text(
                "risk", "swarmfi.last",
                f"{str(payload.get('action','?')).upper()} · risk "
                f"{float(payload.get('risk_score',0)):.1f}/10 · "
                f"conf {round(float(payload.get('confidence',0))*100)}%"
            )

            # AXL: risk → executor (real /send)
            await axl_bus.announce_trade_decision(payload)

            if payload.get("action") == "hold":
                await memories["executor"].log_event(LogEventType.TRADE_FAILED,
                    data={"action": "HOLD", "reason": payload.get("rejection_reason")})
            else:
                # ── Executor ──────────────────────────────────────────────────
                _publish_partial("executing", {
                    "signal": signal, "risk": payload.get("risk_score"),
                    "action": payload.get("action"),
                })
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
                    result_view["tx"]      = result.tx_hash
                    result_view["routing"] = result.routing.value if result.routing else "CLASSIC"
                else:
                    await memories["executor"].update_status(AgentStatus.ERROR, error_message=result.error)
                    await memories["executor"].log_event(LogEventType.TRADE_FAILED, data=result.to_log_data())
                    result_view["error"] = result.error

                # ENS: write the executor's commitment into its text records
                await identity.update_text("executor", "swarmfi.role",   "uniswap_keeperhub")
                await identity.update_text("executor", "swarmfi.status", "idle")
                if result.tx_hash:
                    await identity.update_text("executor", "swarmfi.tx", result.tx_hash)
                    await identity.update_text(
                        "executor", "swarmfi.last",
                        f"{result.routing.value if result.routing else 'CLASSIC'} · "
                        f"tx {result.tx_hash[:12]}…"
                    )

                # AXL: executor → researcher (closes the loop)
                await axl_bus.announce_execution_result({
                    "tx_hash": result.tx_hash,
                    "status":  result.status.value,
                    "routing": result.routing.value if result.routing else None,
                })

            # Commit the buffered swarm state to 0G in ONE on-chain tx
            _publish_partial("committing", {
                "signal": signal, "risk": result_view.get("risk"),
                "action": result_view.get("action"),
                "tx":     result_view.get("tx"),
            })
            snapshot_root: str | None = None
            snapshot_tx:   str | None = None
            try:
                snapshot_root = await asyncio.wait_for(zg.flush(), timeout=240)
                snapshot_tx   = getattr(zg, "snapshot_tx", None)
            except Exception as exc:
                log.warning("snapshot flush failed", error=str(exc))

            # ENS: snapshot proof on every agent's text record (cross-cutting)
            if snapshot_root:
                for role in ("researcher", "risk", "executor"):
                    await identity.update_text(role, "swarmfi.snapshot", snapshot_root)

        _publish_state_view(snapshot_root, result_view, snapshot_tx=snapshot_tx)

    except Exception as exc:
        log.error("trade cycle failed", error=str(exc))


if __name__ == "__main__":
    main()