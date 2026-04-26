#!/usr/bin/env bash
# stop_nodes.sh — gracefully stop all AXL nodes
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDS_DIR="$REPO_ROOT/axl"

GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[nodes]${NC} $*"; }

for AGENT in researcher risk executor; do
    PID_FILE="$PIDS_DIR/$AGENT.pid"
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill -TERM "$PID" && info "Stopped $AGENT (PID $PID)"
        else
            info "$AGENT not running (stale PID $PID)"
        fi
        rm -f "$PID_FILE"
    else
        info "$AGENT — no PID file found, already stopped."
    fi
done

info "Done."