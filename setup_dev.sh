#!/bin/bash
# Fresh development environment setup for Vigil SOC
# Usage: ./setup_dev.sh
source "$(dirname "$0")/scripts/lib.sh"

echo "Vigil SOC - Development Setup"
echo ""

# Prerequisites
WARNINGS=0
command -v docker &>/dev/null || { echo "Warning: Docker not installed."; WARNINGS=$((WARNINGS+1)); }
if ! command -v node &>/dev/null; then
    echo "Warning: Node.js not installed."; WARNINGS=$((WARNINGS+1))
elif ! node -e "process.exit(parseInt(process.version.slice(1))>=18?0:1)" 2>/dev/null; then
    echo "Warning: Node.js 18+ required. Found: $(node --version)"; WARNINGS=$((WARNINGS+1))
fi
[ "$WARNINGS" -gt 0 ] && echo ""

# Environment. Create-only: an existing .env is never rewritten, so an install
# already configured for the DEV_MODE bypass keeps it.
if [ ! -f "$REPO_ROOT/.env" ]; then
    cp "$REPO_ROOT/env.example" "$REPO_ROOT/.env"
    echo "Created .env from env.example (authentication on; first run creates the admin)"
fi
# A fresh setup boots with auth on, which needs a JWT signing secret. Minted
# here so the first ./start.sh finds it; a JWT_SECRET_KEY set in .env still
# wins at start time (start.sh loads .env before calling this).
ensure_jwt_secret

# Python
ensure_venv
install_python_deps
echo "Python dependencies installed."
install_dev_deps

# uv / uvx — several integration MCP servers (crowdstrike, sentinelone,
# pagerduty, aws-security, gcp-*) are launched via `uvx`. Without
# it those servers can't spawn, so the integrations silently never connect.
if ! command -v uvx &>/dev/null && ! [ -x "$HOME/.local/bin/uvx" ]; then
    echo "Installing uv (provides uvx for integration MCP servers)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh || {
        echo "Warning: uv install failed; uvx-based integrations won't spawn."
        WARNINGS=$((WARNINGS+1))
    }
fi

# Frontend
if command -v npm &>/dev/null && [ -d "$REPO_ROOT/clients/web" ]; then
    if [ ! -d "$REPO_ROOT/clients/web/node_modules" ]; then
        (cd "$REPO_ROOT/clients/web" && npm install)
    fi
    echo "Frontend dependencies installed."
fi

# Database
if command -v docker &>/dev/null; then
    ensure_container deeptempo-postgres postgres
    wait_for_postgres || true
    ensure_container deeptempo-redis redis
    echo "Database and Redis running."
fi

# Security detection rules (optional, heavy: ~7,200 rules, ~4GB, 5-10 min).
# Off by default to keep bootstrap fast; opt in with SETUP_DETECTION_REPOS=1.
if [ "${SETUP_DETECTION_REPOS:-0}" = "1" ]; then
    # Don't let this optional, network-heavy step abort the whole setup (set -e).
    if ! "$REPO_ROOT/scripts/setup_detection_repos.sh"; then
        echo ""
        echo "Warning: detection rule setup failed (optional) — continuing."
        echo "Re-run later with: ./scripts/setup_detection_repos.sh"
    fi
else
    echo ""
    echo "Detection rules not installed (optional, ~4GB) — coverage-analysis"
    echo "features stay empty until you run:"
    echo "  SETUP_DETECTION_REPOS=1 ./setup_dev.sh   (or ./scripts/setup_detection_repos.sh)"
fi

echo ""
echo "Setup complete. Run: ./start.sh"
