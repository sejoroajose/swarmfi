#!/usr/bin/env bash
# health_check.sh — verifies every node is reachable and mesh is forming
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
ok()    { echo -e "${GREEN}  ✓${NC} $*"; }
fail()  { echo -e "${RED}  ✗${NC} $*"; FAILED=1; }

FAILED=0
declare -A AGENTS=( [researcher]=9002 [risk]=9012 [executor]=9022 )

echo "=== SwarmFi Node Health Check ==="
echo ""

for AGENT in researcher risk executor; do
    PORT=${AGENTS[$AGENT]}
    TOPO=$(curl -sf "http://127.0.0.1:$PORT/topology" 2>/dev/null) || {
        fail "$AGENT  (port $PORT) — NOT reachable"
        continue
    }
    PUBKEY=$(echo "$TOPO" | python3 -c "import sys,json; print(json.load(sys.stdin)['our_public_key'])" 2>/dev/null)
    ok "$AGENT  port=$PORT  pubkey=${PUBKEY:0:16}…"
done

echo ""
if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}All nodes healthy.${NC}"
else
    echo -e "${RED}Some nodes failed. Check axl/logs/*.log${NC}"
    exit 1
fi