"""
dashboard/server.py
Minimal HTTP server for the SwarmFi live dashboard.

Exposes:
  GET /          → serves dashboard/index.html
  GET /api/state → swarm state from 0G Storage KV (or demo data if offline)
  GET /api/log   → recent log entries

Run:
  pip install fastapi uvicorn
  python3 dashboard/server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))


async def get_swarm_state() -> dict:
    """Read live swarm state from 0G Storage, or return demo data."""
    try:
        from core.storage.client import ZeroGClient
        from core.storage.kv import SwarmKV
        from core.storage.models import KVKey

        async with ZeroGClient.from_env() as zg:
            kv  = SwarmKV(zg)
            raw = await kv.get(KVKey.SWARM_STATE)
            if raw:
                return json.loads(raw.decode())
    except Exception:
        pass

    # Demo data when storage not available
    return {
        "version": 1,
        "updated_at": "2026-04-29T00:00:00Z",
        "agents": {
            "researcher": {"agent_role": "researcher", "status": "scanning",  "last_signal": {"token_in": "ETH", "token_out": "USDC", "signal": "strong"}},
            "risk":       {"agent_role": "risk",       "status": "deciding",  "last_risk_score": 3.2},
            "executor":   {"agent_role": "executor",   "status": "executing", "last_tx_hash": "0xabc..."},
        },
    }


async def get_recent_log() -> list:
    try:
        from core.storage.client import ZeroGClient
        from core.storage.kv import SwarmKV
        from core.storage.log import SwarmLog

        async with ZeroGClient.from_env() as zg:
            kv   = SwarmKV(zg)
            slog = SwarmLog(zg, kv)
            entries = await slog.recent(limit=20)
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


def main() -> None:
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, JSONResponse
        import uvicorn
    except ImportError:
        print("pip install fastapi uvicorn")
        sys.exit(1)

    app   = FastAPI(title="SwarmFi Dashboard")
    root  = Path(__file__).parent

    @app.get("/")
    async def index():
        return FileResponse(root / "index.html")

    @app.get("/api/state")
    async def state():
        return JSONResponse(await get_swarm_state())

    @app.get("/api/log")
    async def log_entries():
        return JSONResponse(await get_recent_log())

    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    print(f"Dashboard running at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()