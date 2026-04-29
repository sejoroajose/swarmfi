"""
demo.py — SwarmFi end-to-end demo

Runs a complete trade cycle:
  Researcher detects signal →
  Risk scores via 0G Compute →
  Executor swaps via Uniswap + KeeperHub →
  Results persisted to 0G Storage →
  Dashboard shows live state

NOTE ON AXL:
  The AXL /send endpoint requires the Yggdrasil routing table to be
  populated (coords != None), which doesn't work on WSL2 kernels.
  This demo bypasses AXL transport by directly calling each agent's
  logic — proving the full application stack works end-to-end.
  When run on real Linux (CI, Codespaces, production), all message
  passing uses real AXL encryption.

Usage:
  python3 demo.py                     # mock mode (no keys needed)
  python3 demo.py --live              # use real APIs (set env vars first)
  python3 demo.py --cycles 3          # run 3 trade cycles
  python3 demo.py --dashboard         # also start the dashboard server

Env vars (all optional for mock mode):
  ZG_PRIVATE_KEY        0G testnet wallet private key
  ZG_COMPUTE_API_KEY    0G Compute app-sk-... key
  ZG_COMPUTE_BASE_URL   0G Compute provider URL
  ZG_COMPUTE_MODEL      e.g. zai-org/GLM-5-FP8
  UNISWAP_API_KEY       Uniswap Trading API key
  KEEPERHUB_API_KEY     KeeperHub API key (kh_...)
  WALLET_ADDRESS        EVM wallet for swaps
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

import structlog

log = structlog.get_logger("demo")


# ── Colour helpers ─────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    BLUE   = "\033[94m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    PURPLE = "\033[95m"
    CYAN   = "\033[96m"
    GREY   = "\033[90m"

def h(text: str, colour: str) -> str:
    return f"{colour}{C.BOLD}{text}{C.RESET}"

def section(title: str) -> None:
    print(f"\n{C.GREY}{'─' * 60}{C.RESET}")
    print(f"  {h(title, C.CYAN)}")
    print(f"{C.GREY}{'─' * 60}{C.RESET}")


# ── Demo runner ────────────────────────────────────────────────────────────────

class SwarmFiDemo:
    def __init__(self, cycles: int = 1) -> None:
        self.cycles = cycles
        self._results: list[dict] = []

    async def run(self) -> None:
        section("SwarmFi — Starting up")
        self._print_config()

        # Initialise all clients
        zg_client      = self._make_zg_client()
        compute_client = self._make_compute_client()
        uniswap_client = self._make_uniswap_client()
        kh_client      = self._make_kh_client()
        ens_identity   = self._make_ens()

        async with zg_client, compute_client, uniswap_client, kh_client:
            # Bootstrap shared memory
            from core.storage.agent_memory import make_shared_memory_set
            memories = make_shared_memory_set(
                ["researcher", "risk", "executor"], zg_client
            )

            # Log startup
            from core.storage.models import AgentStatus, LogEventType
            for role, mem in memories.items():
                await mem.update_status(AgentStatus.IDLE)
                await mem.log_event(LogEventType.AGENT_STARTED,
                                    data={"demo_mode": True})

            section("All agents online")
            print(f"  {h('researcher', C.BLUE)}.swarmfi.eth  — scanning markets")
            print(f"  {h('risk', C.PURPLE)}.swarmfi.eth       — AI risk scoring via 0G Compute")
            print(f"  {h('executor', C.GREEN)}.swarmfi.eth    — guaranteed execution via KeeperHub")

            for i in range(self.cycles):
                if self.cycles > 1:
                    section(f"Trade Cycle {i+1} of {self.cycles}")
                await self._run_cycle(
                    compute_client=compute_client,
                    uniswap_client=uniswap_client,
                    kh_client=kh_client,
                    memories=memories,
                    cycle=i + 1,
                )
                if i < self.cycles - 1:
                    await asyncio.sleep(1)

        section("Demo complete")
        self._print_summary()
        print(f"\n  Run  {h('python3 dashboard/server.py', C.CYAN)}  to see the live dashboard")

    async def _run_cycle(
        self,
        compute_client,
        uniswap_client,
        kh_client,
        memories: dict,
        cycle: int,
    ) -> None:
        from core.storage.models import AgentStatus, LogEventType
        from core.compute.risk_scorer import RiskScorer
        from core.keeperhub.executor import KeeperHubSwapExecutor
        from core.uniswap.models import BaseAddresses
        from core.schema import TradeAction

        started = time.monotonic()

        # ── Step 1: Researcher detects signal ─────────────────────────────────
        print(f"\n{h('① Researcher', C.BLUE)} — scanning market…")
        signal = {
            "token_in":      BaseAddresses.NATIVE_ETH,
            "token_out":     BaseAddresses.USDC,
            "chain_id":      BaseAddresses.BASE_CHAIN_ID,
            "price_usd":     3200.0 + cycle * 50,
            "signal":        "strong" if cycle % 2 == 1 else "medium",
            "reason":        f"RSI divergence detected on Base — cycle {cycle}",
            "amount_in_wei": 100_000_000_000_000_000,  # 0.1 ETH
        }

        await memories["researcher"].update_status(
            AgentStatus.SCANNING, last_signal=signal
        )
        await memories["researcher"].log_event(
            LogEventType.MARKET_SIGNAL, data=signal
        )
        print(f"   Signal: {h(signal['token_in'][:6], C.BLUE)} → "
              f"{h('USDC', C.GREEN)} @ ${signal['price_usd']:.0f} "
              f"[{h(signal['signal'], C.YELLOW)}]")

        # ── Step 2: Risk agent scores via 0G Compute ──────────────────────────
        print(f"\n{h('② Risk Agent', C.PURPLE)} — scoring via 0G Compute (GLM-5-FP8)…")
        await memories["risk"].update_status(
            AgentStatus.DECIDING, last_signal=signal
        )

        scorer      = RiskScorer(compute_client)
        decision_msg = await scorer.score(
            signal=signal,
            sender_pubkey="a" * 64,
            our_pubkey="b" * 64,
        )
        decision = decision_msg.payload

        risk_score = decision.get("risk_score", 0)
        action     = decision.get("action", "hold")
        confidence = decision.get("confidence", 0)

        # Risk bar
        filled = int(risk_score * 3)
        bar    = (
            f"{C.GREEN}{'█' * max(0, 9-filled)}"
            f"{C.YELLOW}{'█' * max(0, min(filled, 3))}"
            f"{C.RED}{'█' * max(0, filled-6)}{C.RESET}"
        )

        print(f"   Risk: [{bar}] {h(f'{risk_score:.1f}/10', C.YELLOW)}")
        print(f"   Action: {h(action.upper(), C.GREEN if action!='hold' else C.RED)}"
              f"  Confidence: {confidence:.0%}")

        await memories["risk"].update_status(
            AgentStatus.IDLE, last_risk_score=risk_score
        )
        await memories["risk"].log_event(
            LogEventType.RISK_DECISION, data=decision
        )

        if action == TradeAction.HOLD or action == "hold":
            reason = decision.get("rejection_reason", "risk gate")
            print(f"   {h('⚠ BLOCKED', C.RED)}: {reason}")
            await memories["executor"].log_event(
                LogEventType.TRADE_FAILED,
                data={"action": "HOLD", "reason": reason},
            )
            self._results.append({
                "cycle": cycle, "action": "hold",
                "risk_score": risk_score, "tx": None
            })
            return

        # ── Step 3: Executor swaps via Uniswap + KeeperHub ────────────────────
        print(f"\n{h('③ Executor', C.GREEN)} — building swap via Uniswap…")
        await memories["executor"].update_status(AgentStatus.EXECUTING)

        executor = KeeperHubSwapExecutor(
            uniswap=uniswap_client,
            keeperhub=kh_client,
            wallet_address="0x" + "a" * 40,
        )
        result = await executor.execute_swap(
            token_in=decision.get("token_in", BaseAddresses.NATIVE_ETH),
            token_out=decision.get("token_out", BaseAddresses.USDC),
            amount_in_wei=str(decision.get("amount_in_wei", 100_000_000_000_000_000)),
            chain_id=decision.get("chain_id", BaseAddresses.BASE_CHAIN_ID),
        )

        elapsed = round(time.monotonic() - started, 2)

        if result.succeeded or result.status.value == "submitted":
            print(f"   {h('✓ EXECUTED', C.GREEN)}")
            print(f"   Tx:      {h(result.tx_hash[:18] + '…' if result.tx_hash else 'dry-run', C.CYAN)}")
            print(f"   Routing: {h(result.routing.value if result.routing else 'CLASSIC', C.BLUE)}")
            if result.gas_used:
                print(f"   Gas:     {result.gas_used:,}")
            print(f"   Time:    {elapsed}s")

            await memories["executor"].update_status(
                AgentStatus.IDLE, last_tx_hash=result.tx_hash
            )
            await memories["executor"].log_event(
                LogEventType.TRADE_EXECUTED, data=result.to_log_data()
            )
            self._results.append({
                "cycle": cycle, "action": action,
                "risk_score": risk_score, "tx": result.tx_hash
            })
        else:
            print(f"   {h('✗ FAILED', C.RED)}: {result.error}")
            await memories["executor"].update_status(
                AgentStatus.ERROR, error_message=result.error
            )
            await memories["executor"].log_event(
                LogEventType.TRADE_FAILED, data=result.to_log_data()
            )
            self._results.append({
                "cycle": cycle, "action": action,
                "risk_score": risk_score, "tx": None, "error": result.error
            })

        # ── Step 4: Researcher reads final swarm state ────────────────────────
        print(f"\n{h('④ Swarm State', C.GREY)} — reading from 0G Storage…")
        swarm = await memories["researcher"].read_swarm_state()
        print(f"   Version: {swarm.version}  Agents: {len(swarm.agents)}")
        history = await memories["researcher"].read_recent_log(limit=3)
        for entry in history[-3:]:
            print(f"   {C.GREY}[{entry['agent_role'][:3]}]{C.RESET} {entry['event_type']}")

    def _print_config(self) -> None:
        import os
        items = [
            ("0G Storage",   "ZG_PRIVATE_KEY",       "live" if os.getenv("ZG_PRIVATE_KEY") else "in-memory"),
            ("0G Compute",   "ZG_COMPUTE_API_KEY",    "live" if os.getenv("ZG_COMPUTE_API_KEY") else "mock"),
            ("Uniswap API",  "UNISWAP_API_KEY",       "live" if os.getenv("UNISWAP_API_KEY") else "mock"),
            ("KeeperHub",    "KEEPERHUB_API_KEY",     "live" if os.getenv("KEEPERHUB_API_KEY") else "mock"),
        ]
        print()
        for name, _, mode in items:
            colour = C.GREEN if mode == "live" else C.GREY
            print(f"  {name:<16} {colour}{mode}{C.RESET}")
        print()

    def _print_summary(self) -> None:
        executed = [r for r in self._results if r.get("tx")]
        held     = [r for r in self._results if r["action"] == "hold"]
        print(f"\n  Cycles:   {len(self._results)}")
        print(f"  Executed: {h(str(len(executed)), C.GREEN)}")
        print(f"  Held:     {h(str(len(held)), C.YELLOW)}")
        if executed:
            avg_risk = sum(r["risk_score"] for r in executed) / len(executed)
            print(f"  Avg risk score on executed trades: {avg_risk:.1f}")

    # ── Client factories ──────────────────────────────────────────────────────

    def _make_zg_client(self):
        from core.storage.client import ZeroGClient
        return ZeroGClient.from_env()

    def _make_compute_client(self):
        from core.compute.client import ZeroGComputeClient
        return ZeroGComputeClient.from_env()

    def _make_uniswap_client(self):
        from core.uniswap.client import UniswapClient
        return UniswapClient.from_env()

    def _make_kh_client(self):
        from core.keeperhub.client import KeeperHubClient
        return KeeperHubClient.from_env()

    def _make_ens(self):
        from core.ens.resolver import AgentIdentity
        return AgentIdentity.from_env()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SwarmFi end-to-end demo")
    parser.add_argument("--cycles",    type=int,  default=1, help="Number of trade cycles")
    parser.add_argument("--dashboard", action="store_true",  help="Also start dashboard server")
    args = parser.parse_args()

    if args.dashboard:
        import subprocess
        subprocess.Popen(
            [sys.executable, "dashboard/server.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Dashboard: {C.CYAN}http://127.0.0.1:8080{C.RESET}")

    asyncio.run(SwarmFiDemo(cycles=args.cycles).run())


if __name__ == "__main__":
    main()