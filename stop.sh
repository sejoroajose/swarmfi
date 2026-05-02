#!/usr/bin/env bash
# Stop dashboard, demo, and AXL nodes — aggressively.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${DASHBOARD_PORT:-8080}"

# Kill anything bound to the dashboard port (covers stale uvicorn workers
# that don't match the dashboard/server.py command line)
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
fi

pkill -9 -f "dashboard/server.py" 2>/dev/null && echo "Dashboard stopped" || echo "Dashboard was not running"
pkill -9 -f "uvicorn"             2>/dev/null || true
pkill -9 -f "demo.py"              2>/dev/null || true

# Stop AXL nodes if the helper exists
if [[ -x "$REPO/scripts/stop_nodes.sh" ]]; then
  "$REPO/scripts/stop_nodes.sh" >/dev/null 2>&1 && echo "AXL nodes stopped" || true
else
  pkill -9 -f "axl/node" 2>/dev/null && echo "AXL nodes stopped" || true
fi

# Tiny pause so the OS releases the socket before any restart attempt
sleep 0.5
echo "Done."
