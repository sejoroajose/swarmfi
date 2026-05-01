#!/usr/bin/env bash
# Stop dashboard, demo, and AXL nodes.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pkill -f "dashboard/server.py" 2>/dev/null && echo "Dashboard stopped" || echo "Dashboard was not running"
pkill -f "demo.py" 2>/dev/null || true

# Stop AXL nodes if the helper exists
if [[ -x "$REPO/scripts/stop_nodes.sh" ]]; then
  "$REPO/scripts/stop_nodes.sh" >/dev/null 2>&1 && echo "AXL nodes stopped" || true
else
  pkill -f "axl/node" 2>/dev/null && echo "AXL nodes stopped" || true
fi
echo "Done."
