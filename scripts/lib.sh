#!/usr/bin/env bash
# scripts/lib.sh — shared helpers for Vigil scripts. Source this, don't execute.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Docker Compose wrapper ---
# Prefer Compose v2 (`docker compose`); fall back to v1 (`docker-compose`)
# for hosts that only ship the standalone binary.
if docker compose version &>/dev/null; then
    _DC_CMD=(docker compose)
elif command -v docker-compose &>/dev/null; then
    _DC_CMD=(docker-compose)
else
    _DC_CMD=(docker compose)  # last resort; surfaces a clear error on use
fi
dc() { "${_DC_CMD[@]}" -f "$REPO_ROOT/docker/docker-compose.yml" "$@"; }

# --- Find Python 3.10+ ---
# Vigil requires Python 3.13+ (claude-agent-sdk). Some integration packages
# (pysnow/ServiceNow, google-cloud-security-command-center, tenable-io) don't
# support 3.13 yet. Those integrations run as MCP servers in isolated processes
# with their own runtime — Vigil core never imports their packages directly.
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    echo "Python 3.10+ is required but not found." >&2
    return 1
}

# --- Build filtered requirements (skip uninitialized submodule editable installs) ---
filtered_reqs() {
    local tmp; tmp=$(mktemp)
    while IFS= read -r line; do
        if [[ "$line" =~ ^-e[[:space:]]+\. ]]; then
            local dir="${line#*-e }"
            dir="${dir#*-e	}"
            [ -f "$dir/setup.py" ] || [ -f "$dir/pyproject.toml" ] || continue
        fi
        echo "$line"
    done < "$REPO_ROOT/requirements.txt" > "$tmp"
    echo "$tmp"
}

# --- Load .env (preserves caller-supplied vars) ---
load_env() {
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a; source "$REPO_ROOT/.env"; set +a
    elif [ -f "$REPO_ROOT/env.example" ]; then
        cp "$REPO_ROOT/env.example" "$REPO_ROOT/.env"
        set -a; source "$REPO_ROOT/.env"; set +a
    fi
}

# --- Ensure venv exists and is activated ---
ensure_venv() {
    local py="${1:-$(find_python)}"
    if [ ! -d "$REPO_ROOT/venv" ]; then
        echo "Creating virtual environment..."
        if ! "$py" -m venv "$REPO_ROOT/venv"; then
            echo "Failed to create virtualenv. The venv module may be missing." >&2
            echo "On Debian/Ubuntu: sudo apt install python3-venv" >&2
            return 1
        fi
    fi
    source "$REPO_ROOT/venv/bin/activate"
}

# --- Install Python deps ---
install_python_deps() {
    pip install -q --upgrade pip
    local reqs; reqs=$(filtered_reqs)
    pip install -q -r "$reqs" || echo "Warning: some packages failed to install."
    rm -f "$reqs"
}

# --- Wait for URL to return 2xx ---
wait_for_url() {
    local url="$1" timeout="${2:-60}" i=0
    while [ $i -lt "$timeout" ]; do
        curl -sf --max-time 2 "$url" >/dev/null 2>&1 && return 0
        sleep 1; i=$((i + 1))
    done
    return 1
}

# --- Ensure a docker service is running ---
ensure_container() {
    local name="$1" service="$2"
    # Anchored exact-name match so e.g. deeptempo-postgres-test doesn't
    # mask a missing deeptempo-postgres.
    if [ -n "$(docker ps -q -f "name=^${name}$")" ]; then
        return 0
    fi
    dc up -d "$service"
}

# --- Wait for postgres readiness ---
wait_for_postgres() {
    local i=0
    while [ $i -lt 30 ]; do
        docker exec deeptempo-postgres pg_isready -U postgres &>/dev/null && return 0
        sleep 1; i=$((i + 1))
    done
    echo "Warning: PostgreSQL may not be ready" >&2
    return 1
}
