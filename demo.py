"""
demo.py — SwarmFi production demo (testnet)

Usage:
  python3 demo.py --cycles 3
  python3 demo.py --pair USDC_ETH --cycles 5
  python3 demo.py --dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Silence debug/info noise from storage layer for clean demo output ─────────
logging.basicConfig(level=logging.WARNING)
for noisy in ("httpx", "httpcore", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

import structlog

# Only show WARNING+ from structlog in demo output
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)
log = structlog.get_logger("demo")


class C:
    RESET="\033[0m";BOLD="\033[1m";BLUE="\033[94m";GREEN="\033[92m"
    YELLOW="\033[93m";RED="\033[91m";PURPLE="\033[95m";CYAN="\033[96m";GREY="\033[90m"

def h(t, c): return f"{c}{C.BOLD}{t}{C.RESET}"
def section(t):
    print(f"\n{C.GREY}{'─'*60}{C.RESET}\n  {h(t, C.CYAN)}\n{C.GREY}{'─'*60}{C.RESET}")


PAIRS = {
    "ETH_USDC":  {"token_in": "0x0000000000000000000000000000000000000000", "token_out": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "in_sym": "ETH",  "out_sym": "USDC",  "chain_id": 8453, "amount_wei": 1_000_000_000_000_000},
    "ETH_USDT":  {"token_in": "0x0000000000000000000000000000000000000000", "token_out": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "in_sym": "ETH",  "out_sym": "USDT",  "chain_id": 8453, "amount_wei": 1_000_000_000_000_000},
    "USDC_ETH":  {"token_in": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "token_out": "0x0000000000000000000000000000000000000000", "in_sym": "USDC", "out_sym": "ETH",   "chain_id": 8453, "amount_wei": 100_000_000},
}


async def fetch_eth_price() -> float:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.coingecko.com/api/v3/simple/price",
                            params={"ids": "ethereum", "vs_currencies": "usd"})
            return float(r.json()["ethereum"]["usd"])
    except Exception:
        return 0.0


class SwarmFiDemo:
    def __init__(self, cycles: int = 1, pair_key: str = "ETH_USDC") -> None:
        self.cycles = cycles
        self.pair   = PAIRS.get(pair_key, PAIRS["ETH_USDC"])
        self._results: list[dict] = []

    async def run(self) -> None:
        section("SwarmFi — Production Testnet Demo")
        self._print_config()

        print(f"  Fetching live ETH price…", end="", flush=True)
        price = await fetch_eth_price()
        print(f" {h(f'${price:,.2f}', C.GREEN) if price else h('(offline — $3200)', C.YELLOW)}")
        if not price:
            price = 3200.0

        zg      = self._make("storage")
        compute = self._make("compute")
        uniswap = self._make("uniswap")
        kh      = self._make("keeperhub")

        self._zg = zg

        async with zg, compute, uniswap, kh:
            from core.storage.agent_memory import make_shared_memory_set
            mems = make_shared_memory_set(["researcher", "risk", "executor"], zg)

            for role, mem in mems.items():
                await self._safe(mem.update_status, self._status("IDLE"))
                await self._safe(mem.log_event, self._evt("AGENT_STARTED"), {"demo": True})

            section("Agents online")
            print(f"  {h('researcher', C.BLUE)}.swarmfi.eth   — market scanner")
            print(f"  {h('risk',       C.PURPLE)}.swarmfi.eth       — 0G Compute AI risk scoring")
            print(f"  {h('executor',   C.GREEN)}.swarmfi.eth   — Uniswap + KeeperHub execution")

            for i in range(self.cycles):
                if self.cycles > 1:
                    section(f"Cycle {i+1} / {self.cycles}")
                await self._cycle(price + i * 20, compute, uniswap, kh, mems, i + 1)
                if i < self.cycles - 1:
                    await asyncio.sleep(1)

            # ── Commit buffered swarm state to 0G in ONE on-chain tx ─────────
            await self._flush_storage(zg)

        section("Summary")
        self._summary()

    async def _cycle(self, price, compute, uniswap, kh, mems, cycle) -> None:
        from core.storage.models import AgentStatus, LogEventType
        from core.compute.risk_scorer import RiskScorer
        from core.keeperhub.executor import KeeperHubSwapExecutor
        from core.scanner import scan_pairs, format_scan_table
        started = time.monotonic()

        # ── Researcher ────────────────────────────────────────────────────────
        print(f"\n{h('① Researcher', C.BLUE)}  scanning Base bluechip pairs · edge profile")
        scan = await scan_pairs(amount_in_wei=self.pair["amount_wei"])
        print(format_scan_table(scan))
        if not scan.best:
            print(f"   {C.YELLOW}⚠ scanner returned no pairs — falling back to default{C.RESET}")
            sig = {
                "token_in": self.pair["token_in"], "token_out": self.pair["token_out"],
                "token_in_sym": self.pair["in_sym"], "token_out_sym": self.pair["out_sym"],
                "chain_id": self.pair["chain_id"], "price_usd": price,
                "signal": "weak",
                "reason": f"Fallback — scanner offline · {self.pair['in_sym']}/{self.pair['out_sym']}",
                "amount_in_wei": self.pair["amount_wei"],
            }
        else:
            sig = scan.best.to_signal_payload(self.pair["amount_wei"])

        await self._safe(mems["researcher"].update_status, self._status("SCANNING"),
                         last_signal=sig)
        await self._safe(mems["researcher"].log_event, self._evt("MARKET_SIGNAL"), sig)
        print(
            f"\n   ★ Pick:  {h(f'{sig['token_in_sym']} → {sig['token_out_sym']}', C.CYAN)}"
            f"   price: {h(f'${sig['price_usd']:,.2f}', C.GREEN)}"
            f"   strength: {h(sig['signal'], C.YELLOW)}"
        )

        # ── Risk ──────────────────────────────────────────────────────────────
        print(f"\n{h('② Risk Agent', C.PURPLE)}  scoring via 0G Compute…")
        await self._safe(mems["risk"].update_status, self._status("DECIDING"))

        scorer   = RiskScorer(compute)
        dec_msg  = await scorer.score(sig, "0" * 64, "0" * 64)
        dec      = dec_msg.payload
        risk     = dec.get("risk_score", 5.0)
        action   = dec.get("action", "hold")
        conf     = dec.get("confidence", 0.5)
        reason   = dec.get("reasoning", "")

        filled = int(risk * 3)
        bar    = (f"{C.GREEN}{'█' * max(0, 9-filled)}"
                  f"{C.YELLOW}{'█' * max(0, min(filled, 3))}"
                  f"{C.RED}{'█' * max(0, filled-9)}{C.RESET}")
        acolour = C.GREEN if action != "hold" else C.RED
        print(f"   Risk:   [{bar}] {h(f'{risk:.1f}/10', C.YELLOW)}")
        print(f"   Action: {h(action.upper(), acolour)}   Confidence: {conf:.0%}")
        if reason:
            print(f"   AI:     {reason[:90]}")

        await self._safe(mems["risk"].update_status, self._status("IDLE"), last_risk_score=risk)
        await self._safe(mems["risk"].log_event, self._evt("RISK_DECISION"), dec)

        # Enrich the signal record with friendly symbols for the dashboard
        sig_view = dict(sig)
        sig_view["token_in_sym"]  = self.pair["in_sym"]
        sig_view["token_out_sym"] = self.pair["out_sym"]

        if action == "hold":
            block_reason = dec.get("rejection_reason", "risk gate")
            print(f"   {h('⚠ BLOCKED', C.RED)}: {block_reason}")
            await self._safe(mems["executor"].log_event, self._evt("TRADE_FAILED"),
                             {"action": "HOLD", "reason": block_reason})
            self._results.append({
                "cycle": cycle, "action": "hold", "risk": risk,
                "confidence": conf, "reasoning": reason,
                "signal": sig_view,
            })
            return

        # ── Executor ──────────────────────────────────────────────────────────
        wallet = os.getenv("WALLET_ADDRESS", "")
        print(f"\n{h('③ Executor', C.GREEN)}  Uniswap price oracle → KeeperHub treasury commitment")
        if not wallet:
            print(f"   {h('ℹ dry-run (set WALLET_ADDRESS for real swaps)', C.YELLOW)}")

        await self._safe(mems["executor"].update_status, self._status("EXECUTING"))
        executor = KeeperHubSwapExecutor(uniswap=uniswap, keeperhub=kh, wallet_address=wallet)
        result = await executor.execute_swap(
            token_in=dec.get("token_in", self.pair["token_in"]),
            token_out=dec.get("token_out", self.pair["token_out"]),
            amount_in_wei=str(dec.get("amount_in_wei", self.pair["amount_wei"])),
            chain_id=dec.get("chain_id", self.pair["chain_id"]),
            wallet_address=wallet,
        )
        elapsed = round(time.monotonic() - started, 2)

        if result.succeeded or result.status.value == "submitted":
            tx = result.tx_hash or (result.error and "queued") or "dry-run"
            label = "✓ EXECUTED" if result.tx_hash else "✓ SUBMITTED to KeeperHub"
            print(f"   {h(label, C.GREEN)}")
            if result.tx_hash:
                print(f"   Tx:      {h(tx[:22]+'…', C.CYAN) if len(tx) > 22 else h(tx, C.CYAN)}")
            else:
                print(f"   Status:  {h('queued · settlement deferred to KH dashboard', C.CYAN)}")
            print(f"   Routing: {result.routing.value if result.routing else 'CLASSIC'}")
            if result.gas_used:
                print(f"   Gas:     {result.gas_used:,}")
            print(f"   Time:    {elapsed}s  (Uniswap quote → KeeperHub broadcast → audit)")
            await self._safe(mems["executor"].update_status, self._status("IDLE"), last_tx_hash=tx)
            await self._safe(mems["executor"].log_event, self._evt("TRADE_EXECUTED"), result.to_log_data())
            self._results.append({
                "cycle": cycle, "action": action, "risk": risk, "tx": tx,
                "confidence": conf, "reasoning": reason,
                "routing": result.routing.value if result.routing else "CLASSIC",
                "signal": sig_view,
            })
        else:
            print(f"   {h('✗ FAILED', C.RED)}: {result.error}")
            await self._safe(mems["executor"].update_status, self._status("ERROR"))
            await self._safe(mems["executor"].log_event, self._evt("TRADE_FAILED"), result.to_log_data())
            self._results.append({
                "cycle": cycle, "action": action, "risk": risk,
                "error": result.error, "confidence": conf, "reasoning": reason,
                "signal": sig_view,
            })

        # ── 0G state readback ─────────────────────────────────────────────────
        print(f"\n{h('④ 0G Storage', C.GREY)}  reading shared swarm state…")
        try:
            swarm   = await asyncio.wait_for(mems["researcher"].read_swarm_state(), timeout=30)
            entries = await asyncio.wait_for(mems["researcher"].read_recent_log(limit=3), timeout=30)
            print(f"   State version: {swarm.version}   Agents: {len(swarm.agents)}")
            for e in entries[-3:]:
                print(f"   {C.GREY}[{e['agent_role'][:3]}]{C.RESET} {e['event_type']}")
        except Exception as exc:
            print(f"   {C.GREY}(skipped: {str(exc)[:50]}){C.RESET}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _safe(self, fn, *args, **kwargs) -> None:
        """Call any async fn, silently swallow 0G timeout/upload errors."""
        try:
            await asyncio.wait_for(fn(*args, **kwargs), timeout=200)
        except asyncio.TimeoutError:
            # Buffered mode means writes are instant; this only fires in
            # legacy every-write-tx mode. Keep quiet — we'll surface via flush.
            pass
        except Exception:
            pass  # storage failures never crash the demo

    async def _flush_storage(self, zg) -> None:
        """Commit all buffered swarm state to 0G as one on-chain snapshot."""
        if not getattr(zg, "is_buffered", False):
            self._snapshot_root = None
            self._snapshot_tx   = None
            self._publish_state_to_dashboard(zg, snapshot_root=None, snapshot_tx=None)
            return
        print(f"\n{h('⑤ 0G Storage', C.GREY)}  committing swarm snapshot to testnet…")
        entries = zg.buffered_entry_count
        try:
            root = await asyncio.wait_for(zg.flush(), timeout=240)
            self._snapshot_root = root
            self._snapshot_tx   = getattr(zg, "snapshot_tx", None)
            if root:
                short = root if len(root) <= 22 else root[:18] + "…"
                tx_short = (self._snapshot_tx[:18] + "…") if self._snapshot_tx else "(deferred)"
                print(f"   {h('✓ committed', C.GREEN)}   "
                      f"entries: {h(str(entries), C.CYAN)}   "
                      f"root: {h(short, C.CYAN)}")
                print(f"   tx:      {h(tx_short, C.CYAN)}")
            else:
                print(f"   {C.GREY}(nothing to commit){C.RESET}")
        except asyncio.TimeoutError:
            self._snapshot_root = None
            self._snapshot_tx   = None
            print(f"   {C.YELLOW}⚠ snapshot tx timed out — buffered state intact locally{C.RESET}")
        except Exception as exc:
            self._snapshot_root = None
            self._snapshot_tx   = None
            print(f"   {C.YELLOW}⚠ snapshot failed: {str(exc)[:60]}{C.RESET}")

        # Publish a local view so the dashboard (separate process) can see state
        self._publish_state_to_dashboard(
            zg,
            snapshot_root=self._snapshot_root,
            snapshot_tx=self._snapshot_tx,
        )

    def _publish_state_to_dashboard(
        self,
        zg,
        snapshot_root: str | None,
        snapshot_tx:   str | None = None,
    ) -> None:
        """
        Write a JSON sidecar with the current swarm view so the dashboard
        process (which has its own memory) can render real state and the
        chat AI can ground its replies in actual data.
        """
        try:
            import json as _json
            from datetime import datetime, timezone
            out_dir = Path(__file__).parent / "logs"
            out_dir.mkdir(parents=True, exist_ok=True)
            view = {
                "updated_at":     datetime.now(tz=timezone.utc).isoformat(),
                "snapshot_root":  snapshot_root,
                "snapshot_tx":    snapshot_tx,
                "cycles":         len(self._results),
                "results":        self._results[-20:],
                "pair":           self.pair,
            }
            (out_dir / "swarmfi-state.json").write_text(_json.dumps(view, indent=2))
        except Exception:
            pass  # never let dashboard publishing crash the demo

    def _status(self, s: str):
        from core.storage.models import AgentStatus
        return {"IDLE": AgentStatus.IDLE, "SCANNING": AgentStatus.SCANNING,
                "DECIDING": AgentStatus.DECIDING, "EXECUTING": AgentStatus.EXECUTING,
                "ERROR": AgentStatus.ERROR}[s]

    def _evt(self, e: str):
        from core.storage.models import LogEventType
        return {"AGENT_STARTED": LogEventType.AGENT_STARTED,
                "MARKET_SIGNAL": LogEventType.MARKET_SIGNAL,
                "RISK_DECISION": LogEventType.RISK_DECISION,
                "TRADE_EXECUTED": LogEventType.TRADE_EXECUTED,
                "TRADE_FAILED": LogEventType.TRADE_FAILED}[e]

    def _print_config(self) -> None:
        keys = [("0G Storage","ZG_PRIVATE_KEY"),("0G Compute","ZG_COMPUTE_API_KEY"),
                ("Uniswap API","UNISWAP_API_KEY"),("KeeperHub","KEEPERHUB_API_KEY")]
        print()
        for name, key in keys:
            live = bool(os.getenv(key, "").strip())
            c = C.GREEN if live else C.GREY
            print(f"  {name:<16} {c}{'live' if live else 'mock'}{C.RESET}")
        print(f"\n  Pair:  {h(self.pair['in_sym']+' → '+self.pair['out_sym'], C.CYAN)}   "
              f"Chain: Base ({self.pair['chain_id']})   "
              f"Amount: {self.pair['amount_wei']} wei\n")

    def _summary(self) -> None:
        executed = [r for r in self._results if r.get("tx")]
        held     = [r for r in self._results if r.get("action") == "hold"]
        failed   = [r for r in self._results if not r.get("tx") and r.get("action") != "hold"]
        print(f"  Cycles: {len(self._results)}  "
              f"Executed: {h(str(len(executed)), C.GREEN)}  "
              f"Held: {h(str(len(held)), C.YELLOW)}"
              + (f"  Failed: {h(str(len(failed)), C.RED)}" if failed else ""))
        if executed:
            avg = sum(r["risk"] for r in executed) / len(executed)
            print(f"  Avg risk on executed trades: {avg:.1f}/10")
        snap = getattr(self, "_snapshot_root", None)
        if snap:
            short = snap if len(snap) <= 26 else snap[:22] + "…"
            print(f"  0G snapshot:  {h(short, C.CYAN)}")
        print(f"\n  Dashboard: {h('http://127.0.0.1:8080', C.CYAN)}\n")

    def _make(self, svc: str):
        if svc == "storage":
            from core.storage.client import ZeroGClient; return ZeroGClient.from_env()
        if svc == "compute":
            from core.compute.client import ZeroGComputeClient; return ZeroGComputeClient.from_env()
        if svc == "uniswap":
            from core.uniswap.client import UniswapClient; return UniswapClient.from_env()
        if svc == "keeperhub":
            from core.keeperhub.client import KeeperHubClient; return KeeperHubClient.from_env()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles",    type=int, default=1)
    parser.add_argument("--pair",      default="ETH_USDC", choices=list(PAIRS.keys()))
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    if args.dashboard:
        import subprocess
        subprocess.Popen([sys.executable, "dashboard/server.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"Dashboard: http://127.0.0.1:8080")

    asyncio.run(SwarmFiDemo(cycles=args.cycles, pair_key=args.pair).run())


if __name__ == "__main__":
    main()