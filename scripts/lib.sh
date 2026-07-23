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
# Containers reach the host-native Ollama via host.docker.internal, but
# load_env exports the host-side OLLAMA_URL and shell env beats the compose
# default - so rewrite it here, at the single container boundary.
_container_ollama_url() {
    echo "${OLLAMA_URL:-http://localhost:11434}" \
        | sed -e 's|//localhost:|//host.docker.internal:|' \
              -e 's|//127\.0\.0\.1:|//host.docker.internal:|' \
              -e 's|//0\.0\.0\.0:|//host.docker.internal:|'
}
dc() {
    OLLAMA_URL="$(_container_ollama_url)" \
        "${_DC_CMD[@]}" -f "$REPO_ROOT/docker/docker-compose.yml" "$@"
}

# --- Ensure the Docker daemon is reachable, launching Docker Desktop if not ---
# `command -v docker` only proves the CLI exists; every compose call still fails
# if the daemon is down. Checks the daemon, starts it, and waits.
docker_daemon_ready() { docker info &>/dev/null; }

ensure_docker() {
    command -v docker &>/dev/null || { echo "Docker is required but not installed." >&2; return 1; }
    docker_daemon_ready && return 0
    echo "Docker daemon not reachable - starting Docker..."
    case "$(uname -s)" in
        Darwin) open -a Docker &>/dev/null || open -a "Docker Desktop" &>/dev/null || true ;;
        Linux)  (systemctl start docker || sudo systemctl start docker) &>/dev/null || true ;;
    esac
    local i=0
    while [ $i -lt 90 ]; do
        docker_daemon_ready && { echo "Docker daemon ready."; return 0; }
        sleep 2; i=$((i + 1))
    done
    echo "Docker daemon did not become ready after 180s. Start Docker and retry." >&2
    return 1
}

# --- Resolve the autostart service list (mirrors services/autostart_config.py) ---
read_autostart() {
    if [ -f "$REPO_ROOT/.vigil-autostart" ]; then
        grep -vE '^\s*(#|$)' "$REPO_ROOT/.vigil-autostart" | tr -d '\r' | tr '\n' ' '
    elif [ -n "${AUTOSTART_SERVICES:-}" ]; then
        echo "${AUTOSTART_SERVICES//,/ }"
    else
        echo "postgres redis bifrost ollama"
    fi
}

# --- Compose service + profile for an autostart name ---
# Mirrors the SERVICES registry; `ollama` is host-native and handled separately.
service_profile() {
    case "$1" in
        pgadmin) echo "dev" ;;
        splunk)  echo "splunk" ;;
        kafka)   echo "kafka" ;;
        jaeger|prometheus|grafana|otel-collector) echo "observability" ;;
        *)       echo "" ;;
    esac
}

service_container() {
    case "$1" in
        otel-collector) echo "deeptempo-otel-collector" ;;
        *) echo "deeptempo-$1" ;;
    esac
}

# --- Start the host-native Ollama (never containerized: no Metal in Docker) ---
# Delegates to services/ollama_process.py rather than reimplementing the spawn:
# macOS has no `setsid`, and a bare `nohup ... &` leaves Ollama in this script's
# process group, where Ctrl+C would kill it. Never fatal - Ollama is optional.
ensure_ollama() {
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m services.ollama_process || \
        echo "Warning: Ollama not started; see logs/ollama.log" >&2
    return 0
}

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

# Ensure npm (and the node beside it) is on PATH. The desktop app spawns these
# scripts with a minimal GUI PATH, so a bare `npm` can miss installs the login
# shell would see — Homebrew, Anaconda, an unactivated conda base, or nvm. A
# no-op when npm already resolves.
ensure_npm_on_path() {
    command -v npm &>/dev/null && return 0
    local d
    for d in /opt/homebrew/bin /usr/local/bin /opt/anaconda3/bin "$HOME/.local/bin"; do
        [ -x "$d/npm" ] && { export PATH="$d:$PATH"; return 0; }
    done
    local nvm_bin
    nvm_bin=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
    [ -n "$nvm_bin" ] && [ -x "$nvm_bin/npm" ] && { export PATH="$nvm_bin:$PATH"; return 0; }
    echo "npm not found — Node.js is required to build the UI." >&2
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
# Profiled services (splunk, kafka, observability...) need COMPOSE_PROFILES set
# or `up` silently no-ops on them.
ensure_container() {
    local name="$1" service="$2" profile="${3:-}"
    # Anchored exact-name match so e.g. deeptempo-postgres-test doesn't
    # mask a missing deeptempo-postgres.
    if [ -n "$(docker ps -q -f "name=^${name}$")" ]; then
        return 0
    fi
    if [ -n "$profile" ]; then
        COMPOSE_PROFILES="$profile" dc up -d "$service"
    else
        dc up -d "$service"
    fi
}

# --- Start every service in the resolved autostart list ---
# postgres/redis/bifrost are prepended unconditionally: the app can't boot
# without them (schema init hard-fails if postgres is down), so a saved list
# that omits them — via a Settings toggle or a hand-edit — must not brick
# startup. Mirrors REQUIRED_SERVICES in services/service_manager.py.
start_autostart_services() {
    local svc profile container seen=" "
    for svc in postgres redis bifrost $(read_autostart); do
        case "$seen" in *" $svc "*) continue ;; esac  # dedupe
        seen="$seen$svc "
        if [ "$svc" = "ollama" ]; then
            ensure_ollama
            continue
        fi
        profile="$(service_profile "$svc")"
        container="$(service_container "$svc")"
        ensure_container "$container" "$svc" "$profile"
        [ "$svc" = "postgres" ] && wait_for_postgres || true
    done
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
