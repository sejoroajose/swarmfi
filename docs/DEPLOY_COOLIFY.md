# Deploying SwarmFi on a Contabo VPS via Coolify

A Coolify-on-Contabo deployment gives SwarmFi a 24/7 public URL that judges can hit any time during the evaluation window — independently of whether your laptop is awake. SwarmFi packs into a single Docker image; Coolify's reverse proxy handles SSL and subdomain routing, so it sits comfortably alongside any other deployments on the same VPS.

---

## What ships in the container, what doesn't

The image runs:

- The FastAPI dashboard (`dashboard/server.py`)
- The 0G Node.js sidecar (called as a subprocess from the dashboard)
- The full `core/*` Python stack — scanner, risk scorer, KeeperHub executor, ENS resolver

The image **deliberately omits** the AXL Go binary. AXL needs a host-network setup (gVisor netstack + IPv6 routing) that is unreliable inside containers. `core/axl_bus` already gracefully no-ops when AXL nodes aren't reachable — every other sponsor primitive (0G Storage, 0G Compute, Uniswap, KeeperHub, ENS) works end-to-end without it. AXL stays a developer-machine demo; it's documented in [`axl/AXL_ROUTING_NOTES.md`](../axl/AXL_ROUTING_NOTES.md).

---

## Prerequisites

| What | Where |
|------|-------|
| Contabo VPS | Any tier with ≥ 2 GB RAM, Ubuntu 22.04 |
| A subdomain pointing at the VPS | e.g. `swarmfi.example.com` → VPS IP (A record) |
| Coolify installed on the VPS | https://coolify.io/docs/installation |
| GitHub repo | Already pushed (`feat/0G-Compute` branch) |

If you don't already have Coolify running on the VPS, install it once:

```bash
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

It exposes its admin dashboard at `http://<VPS-IP>:8000`.

---

## Step-by-step

### 1. In Coolify → New Resource → Application → Public Repository

- **Repository URL:** your GitHub URL (e.g. `https://github.com/sejoroajose/swarmfi`)
- **Branch:** `feat/0G-Compute` (or `main` once merged)
- **Build pack:** **Dockerfile** (Coolify auto-detects)

### 2. Configure the application

- **Port:** `8080` (the Dockerfile exposes this)
- **Domain:** `swarmfi.example.com` (your subdomain) — Coolify provisions SSL via Caddy automatically
- **Health check path:** `/api/state` (already in the Dockerfile)

### 3. Paste environment variables

In Coolify → your application → **Environment Variables**, paste each line below, filling in your real values from `.env`:

```
ZG_PRIVATE_KEY=
ZG_RPC_URL=
ZG_COMPUTE_API_KEY=
ZG_COMPUTE_BASE_URL=https://router-api-testnet.integratenetwork.work/v1
ZG_COMPUTE_MODEL=qwen/qwen-2.5-7b-instruct

UNISWAP_API_KEY=

KEEPERHUB_API_KEY=
KH_KEEPER_ADDRESS=
WALLET_ADDRESS=
WALLET_PRIVATE_KEY=

SWARMFI_COMMITMENT_NETWORK=sepolia
SWARMFI_COMMITMENT_ETH=0.0001

ENS_RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
ENS_PARENT_DOMAIN=swarmfi.eth

RISK_THRESHOLD=7.0
DASHBOARD_PORT=8080
```

Mark `ZG_PRIVATE_KEY`, `WALLET_PRIVATE_KEY`, and `KEEPERHUB_API_KEY` as **secret** so they don't show up in the Coolify UI.

### 4. Deploy

Click **Deploy**. Coolify pulls the repo, runs `docker build`, starts the container, hooks up the reverse proxy, provisions SSL.

First build takes ~3–5 min (it pulls the Python image, installs Node 20, builds the sidecar). Subsequent deploys are ~30 s thanks to Docker layer caching.

### 5. Verify

```bash
curl https://swarmfi.example.com/api/state | jq
curl https://swarmfi.example.com/api/scan  | jq
curl https://swarmfi.example.com/api/agents | jq
```

If `/api/state` returns JSON, you're live.

Visit `https://swarmfi.example.com` in a browser — full dashboard, click **▶ Run a cycle** or toggle **Auto-cycle every 60s**. Each cycle costs ~0.0001 Sepolia ETH from your KeeperHub keeper wallet, so a one-time fund of `0.05 ETH` lasts hundreds of cycles.

---

## Coexisting with other deployments on the same VPS

Coolify isolates every application in its own Docker container behind its own subdomain. SwarmFi's container will share the host with your other deployments without conflict — no port collisions, no resource fights (memory footprint is small: ~150 MB idle, ~250 MB during a cycle).

If you want to run SwarmFi on a non-default port internally (e.g. you want the host's `8080` for something else), adjust the Coolify "port" field — it routes through the reverse proxy regardless.

---

## Sanity checks for judges

After the demo URL is live, share these in your submission:

- **Live demo:** `https://swarmfi.example.com`
- **State endpoint:** `https://swarmfi.example.com/api/state`
- **Edge scan:** `https://swarmfi.example.com/api/scan`
- **ENS profiles:** `https://swarmfi.example.com/api/agents`
- **Latest cycle's tx hash:** click the green tx in the dashboard hero → opens Sepolia Etherscan
- **0G snapshot:** click the snapshot pill in the dashboard header → opens chainscan-galileo

All four URLs return JSON judges can verify without running anything locally.

---

## Local Docker (sanity check before deploy)

```bash
docker build -t swarmfi .
docker run --rm -p 8080:8080 --env-file .env swarmfi

# Or with compose
docker compose up --build
```

Open `http://127.0.0.1:8080`. Same dashboard, running in the exact image Coolify will deploy.

---

## Why not Vercel?

SwarmFi has long-running cycles (~30s each), an out-of-process Node.js sidecar called via stdin/stdout, and a stateful in-process buffered 0G client. Vercel's serverless model (~10s execution, no persistent processes, no shared in-memory state across requests) breaks all three. Coolify-on-VPS is the right shape — a single long-running container, one process, real persistence. Vercel is great for static sites and stateless APIs; this isn't either.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Build fails on `npm ci` | Lockfile mismatch in `zg-sidecar` | The Dockerfile already falls back to `npm install` — re-deploy |
| `/api/state` returns 502 from Coolify | App isn't listening on `0.0.0.0:8080` | The dashboard binds `0.0.0.0` by default; check container logs |
| Cycles run but no tx hash | Keeper wallet empty | Fund `KH_KEEPER_ADDRESS` on Sepolia |
| ENS profiles all empty | `ENS_RPC_URL` unreachable from container | Check the env var, verify the URL works with `curl` from inside the container |
| 0G snapshots fail | Node sidecar can't reach 0G testnet | `docker exec -it swarmfi node zg-sidecar/sidecar.mjs --help` to test |
