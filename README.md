# SwarmFi

> **Three autonomous DeFi agents. One verifiable on-chain ledger. Zero central coordinator.**
>
> SwarmFi is an autonomous DeFi swarm where a researcher agent scans live markets, a risk agent scores opportunities on **0G Compute** (sealed inference), and an executor agent commits decisions through **KeeperHub** with full audit trails on **0G Storage**. Agents communicate peer-to-peer via **Gensyn AXL** and identify themselves through **ENS**.
>
> Built for [ETHGlobal OpenAgents 2026](https://ethglobal.com/events/openagents).

---

## What it does

Every cycle (≈30 s):

1. **Researcher** sweeps four Base bluechip pairs (ETH/USDC, ETH/USDT, WETH/USDC, cbBTC/USDC) and ranks them with a transparent **composite edge profile**:

   ```
   composite = 0.40·momentum + 0.25·bluechip + 0.20·spread + 0.15·size_fit
   ```

2. **Risk** sends the top pair to **0G Compute** (sealed inference, models like `qwen3-plus`, `GLM-FP8`) which scores it 0–10. Anything above the configured threshold is auto-rejected.

3. **Executor** consults the **Uniswap Trading API** as a live price oracle (rate, route, gas estimate) and submits a treasury commitment through **KeeperHub `/api/execute/transfer`**. Real on-chain tx hash returned.

4. **0G Storage** snapshots the entire cycle (signal, decision, tx) as one verifiable Merkle root committed via a flow tx on the 0G testnet.

5. Each agent's ENS profile (`researcher.swarmfi.eth`, etc.) is updated with text records (`swarmfi.status`, `swarmfi.last`, `swarmfi.tx`, `swarmfi.snapshot`) — every read goes through ENS resolution.

---

## Sponsor coverage

| Sponsor    | What it powers in the swarm | File |
|------------|-----------------------------|------|
| **0G**         | `Storage` (buffered KV + log + per-cycle snapshot) and `Compute` (sealed AI risk scoring) | `core/storage/`, `core/compute/`, `zg-sidecar/` |
| **Uniswap**    | Trading API as live price oracle (quote, routing, gas estimate) | `core/uniswap/` |
| **KeeperHub**  | `/api/execute/transfer` as the on-chain commitment rail | `core/keeperhub/` |
| **Gensyn AXL** | P2P agent comms (researcher → risk → executor → researcher) | `core/axl_bus.py`, `axl/` |
| **ENS**        | Agent identity resolution + per-cycle text record updates | `core/ens/` |

Detailed feedback for two sponsor-bounty programs:

- **Uniswap Foundation**: [`FEEDBACK.md`](./FEEDBACK.md)
- **KeeperHub Builder Bounty**: [`core/keeperhub/feedback.md`](./core/keeperhub/feedback.md)
- **Gensyn AXL routing notes**: [`axl/AXL_ROUTING_NOTES.md`](./axl/AXL_ROUTING_NOTES.md)

---

## Architecture

```
                ┌─────────────────────────────────────────────┐
                │              SwarmFi Dashboard              │
                │       (FastAPI, /api/state, /api/scan,      │
                │        /api/agents, /api/axl, /api/chat)    │
                └────────────────┬────────────────────────────┘
                                 │
        ┌────────────┬───────────┼───────────┬─────────────┐
        ▼            ▼           ▼           ▼             ▼
  researcher.eth   risk.eth   executor.eth   AXL bus    ENS resolver
       │             │            │             │            │
       ▼             ▼            ▼             ▼            ▼
   CoinGecko    0G Compute    Uniswap        AXL nodes   .eth → addr
   (prices)    (sealed AI)   Trading API    /send/recv   text records
                                  │
                                  ▼
                              KeeperHub
                          /execute/transfer
                                  │
                                  ▼
                          Sepolia / Base tx
                                  │
                                  ▼
                              0G Storage
                          one snapshot per cycle
                              (chainscan tx)
```

---

## Quick start (local)

### Prerequisites

| Tool       | Version | Notes |
|------------|---------|-------|
| Python     | 3.10+   | Created venv recommended |
| Node.js    | 20+     | For the 0G sidecar |
| Go         | 1.21+   | Only if rebuilding the AXL binary |
| `jq`, `curl` | any   | Diagnostics |

### 1. Clone and configure

```bash
git clone https://github.com/sejoroajose/swarmfi.git
cd swarmfi
cp .env.example .env
```

Fill in `.env`:

```dotenv
# ── 0G ──────────────────────────────────────────────────────
ZG_PRIVATE_KEY=<hex private key for the swarm's 0G wallet>
ZG_COMPUTE_API_KEY=<your 0G Compute key>

# ── Uniswap Trading API ─────────────────────────────────────
UNISWAP_API_KEY=<from developers.uniswap.org>

# ── KeeperHub ───────────────────────────────────────────────
KEEPERHUB_API_KEY=<kh_… from app.keeperhub.com>
KH_KEEPER_ADDRESS=<your KH-managed wallet address — fund with Sepolia ETH>

# Treasury commitment per cycle (default 0.0001 ETH)
SWARMFI_COMMITMENT_ETH=0.0001
SWARMFI_COMMITMENT_NETWORK=sepolia    # or "ethereum" for mainnet

# ── ENS (optional — falls back to deterministic mock) ───────
ENS_RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
ENS_PARENT_DOMAIN=swarmfi.eth
```

### 2. Install Python deps

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
# or, without the package install:
pip install httpx pydantic tenacity structlog fastapi 'uvicorn[standard]' \
            eth-abi eth-account 'web3<7' ens
```

### 3. Build the 0G sidecar

The 0G TypeScript SDK collides with our `core` package, so we run it as a Node.js sidecar:

```bash
cd zg-sidecar
npm install
cd ..
```

That's it — no compile step, the sidecar is plain ESM JavaScript.

### 4. Start everything

```bash
chmod +x start.sh stop.sh scripts/*.sh
./start.sh --live
```

This will:

- Boot the three AXL nodes (researcher · risk · executor)
- Start the dashboard at `http://127.0.0.1:8080`
- Run one demo cycle in the terminal

### 5. Run more cycles

From the dashboard, click **▶ Run a cycle** — or toggle **Auto-cycle every 60s** for hands-off operation.

From the CLI:

```bash
python3 demo.py --cycles 5 --pair ETH_USDC
```

### 6. Stop

```bash
./stop.sh
```

---

## Funding the keeper wallet

Before any cycle can settle on-chain you need to fund the KeeperHub-managed wallet (whose address you set as `KH_KEEPER_ADDRESS`).

For Sepolia (the default `SWARMFI_COMMITMENT_NETWORK`):

| Faucet | URL |
|--------|-----|
| Alchemy | https://sepoliafaucet.com |
| Infura | https://www.infura.io/faucet/sepolia |
| Google Cloud | https://cloud.google.com/application/web3/faucet/ethereum/sepolia |
| QuickNode | https://faucet.quicknode.com/ethereum/sepolia |

`0.05 Sepolia ETH` is plenty for hundreds of demo cycles at the default `0.0001 ETH` commitment size.

---

## Verifying it's real

```bash
# Latest cycle
curl -s http://127.0.0.1:8080/api/state | jq

# Multi-pair edge scan (live CoinGecko prices, 30 s TTL cache)
curl -s http://127.0.0.1:8080/api/scan | jq

# ENS-backed agent profiles (resolved live every poll)
curl -s http://127.0.0.1:8080/api/agents | jq

# AXL inter-node send events
curl -s http://127.0.0.1:8080/api/axl | jq
```

Each cycle produces three on-chain artifacts:

1. **KeeperHub commitment** on Sepolia/Base — `Last tx` field, links to Etherscan
2. **0G Storage snapshot** on the 0G testnet — `0G snapshot` pill in the header, links to chainscan-galileo
3. **ENS text-record updates** for each of the three agents (mock or live)

---

## Repository layout

```
swarmfi/
├── core/
│   ├── axl_bus.py           # AXL inter-node broadcasts
│   ├── axl_client.py        # Thin async client over AXL's HTTP API
│   ├── ens/
│   │   └── resolver.py      # AgentIdentity + ENS text-record management
│   ├── compute/
│   │   ├── client.py        # 0G Compute (sealed inference)
│   │   └── risk_scorer.py   # AI risk scoring + bluechip token registry
│   ├── keeperhub/
│   │   ├── client.py        # KH HTTP API
│   │   ├── executor.py      # Path B — Uniswap quote + KH /execute/transfer
│   │   └── feedback.md      # KeeperHub Builder Bounty feedback
│   ├── scanner.py           # Multi-pair edge profile scanner
│   ├── storage/
│   │   ├── client.py        # 0G Storage (buffered + snapshot flush)
│   │   ├── kv.py / log.py   # KV + append-only log on top of buffer
│   │   └── agent_memory.py  # AgentMemory wrapper
│   └── uniswap/             # Trading API client (quote/swap/check_approval)
├── agents/                  # Long-running agent classes (used by AXL connectivity tests)
├── axl/
│   ├── node                 # AXL Go binary
│   ├── configs/             # Per-role node configs
│   ├── keys/                # Private keys for each AXL node
│   └── AXL_ROUTING_NOTES.md # Honest routing-layer notes
├── dashboard/
│   ├── server.py            # FastAPI app
│   └── index.html           # Single-file cinematic UI
├── zg-sidecar/
│   ├── sidecar.mjs          # Node.js wrapper around @0gfoundation/0g-ts-sdk
│   └── package.json
├── scripts/
│   ├── start_nodes.sh       # Launch AXL nodes
│   ├── stop_nodes.sh        # Kill AXL nodes
│   ├── health_check.sh      # Verify topology + connectivity
│   └── setup.sh             # First-time setup
├── tests/                   # 200+ unit + integration tests
├── demo.py                  # CLI cycle runner
├── start.sh                 # One-shot launcher (AXL + dashboard + demo cycle)
├── stop.sh                  # Stop everything
├── FEEDBACK.md              # Uniswap Foundation prize feedback
└── pyproject.toml
```

---

## Tests

```bash
pytest                              # all unit tests
pytest -m integration               # AXL connectivity (requires running nodes)
pytest tests/test_keeperhub_executor.py -v
```

CI runs unit + KeeperHub mock + connectivity-topology tests on every push. AXL P2P send tests are skipped on CI (gVisor netstack routing isn't reliable in GitHub-hosted runners — see `axl/AXL_ROUTING_NOTES.md`).

---

## Configuration reference

| Env var                       | Default                                | Description |
|-------------------------------|----------------------------------------|-------------|
| `ZG_PRIVATE_KEY`              | (required for live 0G)                 | Hex private key for the swarm's 0G wallet |
| `ZG_COMPUTE_API_KEY`          | (required for live AI)                 | 0G Compute auth |
| `ZG_COMPUTE_MODEL`            | `qwen/qwen-2.5-7b-instruct`            | Model id for sealed inference |
| `UNISWAP_API_KEY`             | (required for live quotes)             | Uniswap Trading API |
| `KEEPERHUB_API_KEY`           | (required for execution)               | `kh_…` org key |
| `KH_KEEPER_ADDRESS`           | (required)                             | The KH-managed wallet that signs commitments |
| `SWARMFI_COMMITMENT_ETH`      | `0.0001`                               | ETH per cycle commitment |
| `SWARMFI_COMMITMENT_NETWORK`  | `sepolia`                              | Where commitments settle |
| `SWARMFI_BUFFERED`            | `1`                                    | Buffered 0G mode (one tx per cycle) |
| `ENS_RPC_URL`                 | `https://eth.llamarpc.com`             | RPC for live ENS resolution |
| `ENS_PARENT_DOMAIN`           | `swarmfi.eth`                          | Parent ENS domain for the swarm |
| `RISK_THRESHOLD`              | `7.0`                                  | Above this score, swarm holds |
| `DASHBOARD_PORT`              | `8080`                                 | Dashboard HTTP port |

---

## Status & known limitations

- ✓ End-to-end: scan → score → quote → commit → snapshot, every cycle, fully on-chain
- ✓ Real green txs on Sepolia Etherscan + 0G chainscan
- ✓ Multi-pair edge scoring with transparent rubric
- ✓ Buffered 0G storage with one consolidated snapshot per cycle
- ✓ ENS-resolved agent identities + per-cycle text-record updates
- ⚠ AXL P2P send routing is unreliable on localhost / WSL — see `axl/AXL_ROUTING_NOTES.md`. Topology, registry bootstrap, and `/topology` work; the gVisor-netstack dial path is configurable but not yet healthy in this environment.
- ⚠ KeeperHub `/api/execute/contract-call` has two reproducible bugs documented in `core/keeperhub/feedback.md`. We pivoted to `/api/execute/transfer` (Path B) which works end-to-end.

---

## License

MIT. Built during ETHGlobal OpenAgents 2026.
