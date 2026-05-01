#!/usr/bin/env bash
# start.sh — place this in your REPO ROOT (/mnt/d/swarmfi/start.sh)
set -euo pipefail

# Always resolve to repo root, regardless of where you call this from
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# If called from scripts/ subdirectory, go up one level
[[ "$(basename "$REPO")" == "scripts" ]] && REPO="$(dirname "$REPO")"
cd "$REPO"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
info() { echo -e "${CYAN}  →${NC} $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; exit 1; }

CYCLES=1; PAIR="ETH_USDC"; LIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live)   LIVE=1 ;;
    --cycles) CYCLES="$2"; shift ;;
    --pair)   PAIR="$2"; shift ;;
    *) warn "Unknown arg: $1" ;;
  esac; shift
done

echo ""
echo -e "${CYAN}${BOLD}  ███████╗██╗    ██╗ █████╗ ██████╗ ███╗   ███╗███████╗██╗${NC}"
echo -e "${CYAN}${BOLD}  ██╔════╝██║    ██║██╔══██╗██╔══██╗████╗ ████║██╔════╝██║${NC}"
echo -e "${CYAN}${BOLD}  ███████╗██║ █╗ ██║███████║██████╔╝██╔████╔██║█████╗  ██║${NC}"
echo -e "${CYAN}${BOLD}  ╚════██║██║███╗██║██╔══██║██╔══██╗██║╚██╔╝██║██╔══╝  ██║${NC}"
echo -e "${CYAN}${BOLD}  ███████║╚███╔███╔╝██║  ██║██║  ██║██║ ╚═╝ ██║██║     ██║${NC}"
echo -e "${CYAN}${BOLD}  ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝     ╚═╝${NC}"
echo ""
echo -e "  ETHGlobal OpenAgents 2026 · Autonomous DeFi Swarm"
echo ""

# ── Load .env from repo root ──────────────────────────────────────────────────
if [[ -f "$REPO/.env" ]]; then
  set -a; source "$REPO/.env"; set +a
  ok ".env loaded from $REPO/.env"
else
  warn "No .env found at $REPO/.env — running in mock mode"
  warn "Copy .env.example to .env and fill in your keys"
fi

# ── Python check ──────────────────────────────────────────────────────────────
PYTHON=""
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1; then
    MAJOR=$("$p" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
    [[ "$MAJOR" -ge 10 ]] && PYTHON="$p" && break
  fi
done
[[ -z "$PYTHON" ]] && fail "Python 3.10+ not found"
ok "Python $($PYTHON --version 2>&1)"

# ── Install missing deps silently ─────────────────────────────────────────────
NEED=""
for pkg in httpx pydantic tenacity structlog fastapi uvicorn; do
  "$PYTHON" -c "import $pkg" 2>/dev/null || NEED="$NEED $pkg"
done
if [[ -n "$NEED" ]]; then
  info "Installing:$NEED"
  "$PYTHON" -m pip install $NEED --break-system-packages -q >/dev/null 2>&1
fi
ok "Python deps ready"
echo ""

# ── Show API status ───────────────────────────────────────────────────────────
for svc in "0G Storage:ZG_PRIVATE_KEY" "0G Compute:ZG_COMPUTE_API_KEY" "Uniswap API:UNISWAP_API_KEY" "KeeperHub:KEEPERHUB_API_KEY"; do
  name="${svc%%:*}"; key="${svc##*:}"; val="${!key:-}"
  [[ -n "$val" ]] \
    && echo -e "  ${GREEN}●${NC} $name  ${GREEN}live${NC}" \
    || echo -e "  ${YELLOW}○${NC} $name  ${YELLOW}mock${NC}"
done
echo ""

# ── Kill stale dashboard ──────────────────────────────────────────────────────
pkill -f "dashboard/server.py" 2>/dev/null || true
pkill -f "uvicorn" 2>/dev/null || true
sleep 0.3

# ── Start dashboard ───────────────────────────────────────────────────────────
info "Starting dashboard…"
mkdir -p "$REPO/logs"
PYTHONPATH="$REPO" "$PYTHON" "$REPO/dashboard/server.py" \
  > "$REPO/logs/dashboard.log" 2>&1 &
DASH_PID=$!

# Wait up to 6 s for dashboard to be ready
for i in $(seq 1 20); do
  sleep 0.3
  curl -sf http://127.0.0.1:8080/ >/dev/null 2>&1 && break
done

if curl -sf http://127.0.0.1:8080/ >/dev/null 2>&1; then
  ok "Dashboard → ${CYAN}http://127.0.0.1:8080${NC}"
else
  warn "Dashboard slow to start — check ./logs/dashboard.log"
fi

# Try to open browser (WSL2 / Linux / macOS)
for opener in wslview xdg-open open; do
  command -v "$opener" >/dev/null 2>&1 && "$opener" "http://127.0.0.1:8080" 2>/dev/null & break
done

echo ""
info "Running ${CYAN}$CYCLES${NC} cycle(s) · pair: ${CYAN}$PAIR${NC}"
echo ""

# ── Run demo (suppress debug logs, show only INFO+) ───────────────────────────
PYTHONPATH="$REPO" "$PYTHON" "$REPO/demo.py" --cycles "$CYCLES" --pair "$PAIR"

echo ""
echo -e "  ${GREEN}Done.${NC}  Dashboard: ${CYAN}http://127.0.0.1:8080${NC}"
echo -e "  Stop with: ${CYAN}./stop.sh${NC}  or  ${CYAN}Ctrl+C${NC}"
echo ""

wait $DASH_PID 2>/dev/null || true