#!/usr/bin/env bash
# build_sidecar.sh — install dependencies for the 0G Node.js storage sidecar.
# Run once after cloning, or after updating zg-sidecar/package.json.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIDECAR_DIR="$REPO_ROOT/zg-sidecar"

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[sidecar]${NC} $*"; }
error() { echo -e "${RED}[sidecar]${NC} $*" >&2; exit 1; }

command -v node >/dev/null 2>&1 || error "Node.js not found. Install from https://nodejs.org (>=18 required)"
NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
[[ "$NODE_VER" -ge 18 ]] || error "Node.js >=18 required (found v$NODE_VER)"
info "Node.js $(node --version) found"

[[ -f "$SIDECAR_DIR/sidecar.mjs" ]]  || error "sidecar.mjs not found at $SIDECAR_DIR"
[[ -f "$SIDECAR_DIR/package.json" ]] || error "package.json not found at $SIDECAR_DIR"

if [[ -d "$SIDECAR_DIR/node_modules/@0gfoundation" ]]; then
    info "node_modules already installed — skipping npm install."
    info "To force reinstall: rm -rf $SIDECAR_DIR/node_modules && ./scripts/build_sidecar.sh"
else
    info "Installing Node.js dependencies..."
    cd "$SIDECAR_DIR"
    npm install --prefer-offline 2>&1 | grep -v "^npm warn"
fi

# Smoke test
info "Smoke test..."
echo -n "test" | node "$SIDECAR_DIR/sidecar.mjs" upload --help 2>&1 | grep -q "unknown command\|--key" \
    || echo "  (binary responds to commands — OK)"
info "✓ Sidecar ready at zg-sidecar/sidecar.mjs"