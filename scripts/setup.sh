#!/usr/bin/env bash
# setup.sh — one-time setup: clone AXL, build binary, generate keys
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AXL_DIR="$REPO_ROOT/axl"
KEYS_DIR="$AXL_DIR/keys"
AXL_REPO_DIR="$AXL_DIR/axl-repo"

# ── colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[setup]${NC} $*"; }
error() { echo -e "${RED}[setup]${NC} $*" >&2; exit 1; }

# ── prereqs ──────────────────────────────────────────────────────────────────
check_prereqs() {
    info "Checking prerequisites…"
    command -v go    >/dev/null 2>&1 || error "Go is not installed. See https://go.dev/dl/"
    command -v git   >/dev/null 2>&1 || error "git is required"
    command -v python3 >/dev/null 2>&1 || error "Python 3.10+ is required"

    GO_VERSION=$(go version | awk '{print $3}' | sed 's/go//')
    REQUIRED="1.25"
    if [[ "$(printf '%s\n' "$REQUIRED" "$GO_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]]; then
        warn "Go $GO_VERSION detected; AXL recommends 1.25.x. Build may fail with 1.26+."
        warn "If it does: prefix make build with GOTOOLCHAIN=go1.25.5"
    fi

    # Detect which openssl supports ed25519
    if openssl genpkey -algorithm ed25519 -help >/dev/null 2>&1; then
        OPENSSL_BIN="openssl"
    elif command -v /opt/homebrew/opt/openssl/bin/openssl >/dev/null 2>&1; then
        OPENSSL_BIN="/opt/homebrew/opt/openssl/bin/openssl"
        info "Using Homebrew OpenSSL at $OPENSSL_BIN"
    else
        error "No openssl with ed25519 support found. On macOS run: brew install openssl"
    fi
    export OPENSSL_BIN
}

# ── clone & build AXL ────────────────────────────────────────────────────────
build_axl() {
    if [[ -f "$AXL_DIR/node" ]]; then
        info "AXL binary already exists at axl/node — skipping build."
        return
    fi

    info "Cloning gensyn-ai/axl…"
    git clone --depth 1 https://github.com/gensyn-ai/axl.git "$AXL_REPO_DIR"

    info "Building AXL node binary…"
    pushd "$AXL_REPO_DIR" >/dev/null
    # Use explicit toolchain flag in case user has Go 1.26+
    GOTOOLCHAIN=go1.25.5 go build -o "$AXL_DIR/node" ./cmd/node/ \
        || go build -o "$AXL_DIR/node" ./cmd/node/
    popd >/dev/null

    info "Binary built → axl/node"
}

# ── generate keys ────────────────────────────────────────────────────────────
generate_keys() {
    mkdir -p "$KEYS_DIR"

    for AGENT in researcher risk executor; do
        KEY_FILE="$KEYS_DIR/$AGENT.pem"
        if [[ -f "$KEY_FILE" ]]; then
            info "Key for $AGENT already exists — skipping."
        else
            info "Generating ed25519 key for $AGENT…"
            $OPENSSL_BIN genpkey -algorithm ed25519 -out "$KEY_FILE" 2>/dev/null
            chmod 600 "$KEY_FILE"
            info "  → axl/keys/$AGENT.pem"
        fi
    done
}

# ── python deps ──────────────────────────────────────────────────────────────
install_python_deps() {
    info "Installing Python dependencies…"
    python3 -m pip install --quiet -e ".[dev]"
    info "Python deps installed."
}

# ── .env.example ─────────────────────────────────────────────────────────────
create_env_example() {
    ENV_EXAMPLE="$REPO_ROOT/.env.example"
    if [[ ! -f "$ENV_EXAMPLE" ]]; then
        cat > "$ENV_EXAMPLE" <<'EOF'
# Copy to .env and fill in values
# AXL
RESEARCHER_API=http://127.0.0.1:9002
RISK_API=http://127.0.0.1:9012
EXECUTOR_API=http://127.0.0.1:9022

# 0G (Stage 2)
ZG_RPC_URL=
ZG_PRIVATE_KEY=

# Uniswap (Stage 3)
UNISWAP_API_KEY=

# KeeperHub (Stage 4)
KEEPERHUB_API_KEY=
EOF
        info "Created .env.example"
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────
main() {
    info "=== SwarmFi Stage 1 Setup ==="
    check_prereqs
    build_axl
    generate_keys
    install_python_deps
    create_env_example
    echo ""
    info "✓ Setup complete."
    info "  Next: run  ./scripts/start_nodes.sh"
    info "  Then: run  pytest tests/ -m unit"
    info "  Then: run  pytest tests/ -m integration"
}

main "$@"