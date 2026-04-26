#!/usr/bin/env bash
# start_nodes.sh — launch all three AXL nodes in the background
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AXL_BIN="$REPO_ROOT/axl/node"
CONFIGS_DIR="$REPO_ROOT/axl/configs"
LOGS_DIR="$REPO_ROOT/axl/logs"
PIDS_DIR="$REPO_ROOT/axl"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[nodes]${NC} $*"; }
warn()  { echo -e "${YELLOW}[nodes]${NC} $*"; }
error() { echo -e "${RED}[nodes]${NC} $*" >&2; exit 1; }

[[ -f "$AXL_BIN" ]] || error "AXL binary not found at axl/node. Run ./scripts/setup.sh first."

mkdir -p "$LOGS_DIR"

start_node() {
    local NAME=$1
    local CONFIG=$2
    local API_PORT=$3
    local PID_FILE="$PIDS_DIR/$NAME.pid"

    # Kill existing instance if stale PID exists
    if [[ -f "$PID_FILE" ]]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            warn "$NAME already running (PID $OLD_PID). Skipping."
            return
        fi
        rm -f "$PID_FILE"
    fi

    info "Starting $NAME node (api=:$API_PORT)…"
    # Run from repo root so relative key paths in configs resolve correctly
    pushd "$REPO_ROOT" >/dev/null
    "$AXL_BIN" -config "$CONFIG" \
        > "$LOGS_DIR/$NAME.log" 2>&1 &
    echo $! > "$PID_FILE"
    popd >/dev/null

    # Wait up to 5 s for the API to become reachable
    local TRIES=0
    until curl -sf "http://127.0.0.1:$API_PORT/topology" >/dev/null 2>&1; do
        sleep 0.5
        TRIES=$((TRIES + 1))
        if [[ $TRIES -ge 10 ]]; then
            error "$NAME did not become healthy after 5 s. Check axl/logs/$NAME.log"
        fi
    done
    info "  ✓ $NAME is healthy (PID $(cat "$PID_FILE"))"
}

start_mcp_router() {
    local NAME=$1
    local PORT=$2
    local PID_FILE="$PIDS_DIR/${NAME}_mcp.pid"

    info "Starting $NAME MCP router (port=$PORT)…"
    pushd "$REPO_ROOT" >/dev/null
    python3 -m mcp_routing.mcp_router --port "$PORT" \
        > "$LOGS_DIR/${NAME}_mcp.log" 2>&1 &
    echo $! > "$PID_FILE"
    popd >/dev/null
    sleep 1
    info "  ✓ $NAME MCP router running (PID $(cat "$PID_FILE"))"
}

info "=== Starting SwarmFi AXL nodes ==="

# Researcher first — it is the hub that risk + executor peer to
start_node "researcher" "$CONFIGS_DIR/researcher.json" "9002"

# Brief pause so researcher is accepting connections before spokes dial in
sleep 1

start_node "risk"       "$CONFIGS_DIR/risk.json"       "9012"
start_node "executor"   "$CONFIGS_DIR/executor.json"   "9022"

start_mcp_router "researcher" "9003"
start_mcp_router "risk"       "9013"
start_mcp_router "executor"   "9023"

info ""
info "All nodes running. Topology:"
for AGENT in researcher risk executor; do
    PORT_MAP=( researcher:9002 risk:9012 executor:9022 )
    PORT=$(echo "${PORT_MAP[@]}" | tr ' ' '\n' | grep "^$AGENT:" | cut -d: -f2)
    PUB=$(curl -sf "http://127.0.0.1:$PORT/topology" \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['our_public_key'])" 2>/dev/null || echo "unavailable")
    info "  $AGENT  port=$PORT  pubkey=$PUB"
done

info ""
info "Run  ./scripts/health_check.sh  to verify connectivity."
info "Stop with  ./scripts/stop_nodes.sh"