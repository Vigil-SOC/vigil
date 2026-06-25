#!/bin/bash
# Fresh development environment setup for Vigil SOC
# Usage: ./setup_dev.sh
source "$(dirname "$0")/scripts/lib.sh"

echo "Vigil SOC - Development Setup"
echo ""

# Prerequisites
PYTHON=$(find_python)

WARNINGS=0
command -v docker &>/dev/null || { echo "Warning: Docker not installed."; WARNINGS=$((WARNINGS+1)); }
if ! command -v node &>/dev/null; then
    echo "Warning: Node.js not installed."; WARNINGS=$((WARNINGS+1))
elif ! node -e "process.exit(parseInt(process.version.slice(1))>=18?0:1)" 2>/dev/null; then
    echo "Warning: Node.js 18+ required. Found: $(node --version)"; WARNINGS=$((WARNINGS+1))
fi
[ "$WARNINGS" -gt 0 ] && echo ""

# Environment
if [ ! -f "$REPO_ROOT/.env" ]; then
    cp "$REPO_ROOT/env.example" "$REPO_ROOT/.env"
    echo "Created .env from env.example (DEV_MODE=true)"
fi

# Python
ensure_venv "$PYTHON"
install_python_deps
echo "Python dependencies installed."

# Frontend
if command -v npm &>/dev/null && [ -d "$REPO_ROOT/frontend" ]; then
    if [ ! -d "$REPO_ROOT/frontend/node_modules" ]; then
        (cd "$REPO_ROOT/frontend" && npm install)
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

echo ""
echo "Setup complete. Run: ./start.sh"
