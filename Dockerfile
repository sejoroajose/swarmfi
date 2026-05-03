# SwarmFi container
#
# Single-image deploy — FastAPI dashboard + Node.js sidecar in one container.
# AXL nodes are intentionally NOT included (the Go binary is Linux-host-network
# specific and unreliable in containers; see axl/AXL_ROUTING_NOTES.md). The
# dashboard's `core.axl_bus` gracefully no-ops when AXL nodes aren't reachable —
# every other sponsor (0G, Uniswap, KeeperHub, ENS) works end-to-end without it.
#
# Build:    docker build -t swarmfi .
# Run:      docker run --rm -p 8080:8080 --env-file .env swarmfi
# Coolify:  point at this Dockerfile, expose 8080, paste env vars in the UI.

FROM python:3.12-slim AS base

# Node.js 20 for the 0G sidecar
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
        gcc g++ make python3-dev \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (cached layer) ────────────────────────────────────────────────
#
# Belt-and-suspenders against the pysha3-needs-gcc trap:
#   1. `web3>=7,<8` → modern web3 dropped pysha3 in favour of pycryptodome,
#      so 99% of the time no C compiler is needed at all.
#   2. gcc + python3-dev are still installed above so the 1% case (any other
#      transitive that happens to ship a sdist) still builds cleanly.
COPY pyproject.toml ./
# IMPORTANT: do NOT `pip install ens` separately. The standalone `ens`
# PyPI package is abandoned and only works with web3<4 (which would drag
# in pysha3 → gcc dance from hell). Modern web3>=7 ships its own `ens`
# submodule, so `from ens import ENS` works out of the box.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
        httpx pydantic tenacity structlog \
        fastapi "uvicorn[standard]" \
        eth-abi eth-account "web3>=7,<8"
        
RUN apt-get update && apt-get install -y jq

# ── 0G sidecar (cached layer) ─────────────────────────────────────────────────
COPY zg-sidecar/package*.json zg-sidecar/
RUN cd zg-sidecar && npm ci --omit=dev || npm install --omit=dev

# ── App code ──────────────────────────────────────────────────────────────────
COPY core/      core/
COPY agents/    agents/
COPY dashboard/ dashboard/
COPY zg-sidecar/sidecar.mjs zg-sidecar/sidecar.mjs
COPY demo.py    ./

# Where the dashboard writes the in-progress + cycle state file
RUN mkdir -p /app/logs

# Coolify / generic PaaS friendliness
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_PORT=8080

EXPOSE 8080

# Health check used by Coolify / docker-compose
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:8080/api/state >/dev/null || exit 1

CMD ["python3", "dashboard/server.py"]
