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
# PYTHON_VERSION feeds the images' base-image build arg. Exported here for the
# same reason as OLLAMA_URL: compose substitutes from the environment and cannot
# read .python-version itself, so the pin is injected at the one boundary where
# compose is invoked, rather than copied into the compose file.
dc() {
    OLLAMA_URL="$(_container_ollama_url)" \
    PYTHON_VERSION="$(python_pin 2>/dev/null || true)" \
        "${_DC_CMD[@]}" -f "$REPO_ROOT/infra/docker/docker-compose.yml" "$@"
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
# Delegates to scripts/ollama_supervise.py rather than reimplementing the spawn:
# macOS has no `setsid`, and a bare `nohup ... &` leaves Ollama in this script's
# process group, where Ctrl+C would kill it. Never fatal - Ollama is optional.
ensure_ollama() {
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        python3 "$REPO_ROOT/scripts/ollama_supervise.py" || \
        echo "Warning: Ollama not started; see logs/ollama.log" >&2
    return 0
}

# --- Python toolchain ---
# Vigil provisions its own interpreter instead of discovering one on PATH.
# Discovery picks up whatever the user's shell happens to expose — a conda env,
# a pyenv shim, an x86_64 build running under Rosetta — and a bad match doesn't
# fail here: it fails minutes later as a missing wheel or a source build against
# a toolchain that isn't installed. uv downloads a standalone CPython matching
# .python-version and the host's real architecture, so every machine (macOS or
# Linux, arm64 or x86_64, conda or not) ends up with the same interpreter.

# The pinned version. One answer for everything that shares core/: this venv,
# both Docker images, and CI.
python_pin() {
    tr -d '[:space:]' < "$REPO_ROOT/.python-version"
}

# uv provisions interpreters matching *its own* architecture, not the host's:
# an x86_64 uv downloads an x86_64 CPython even with --python-preference
# only-managed. So a uv found on PATH is only trusted when its target triple
# agrees with uname -m. Found the hard way — a conda-installed Intel uv on an
# Apple Silicon Mac provisioned an Intel 3.12, and cryptography then failed to
# build against a Rust target that wasn't installed.
uv_arch_ok() {
    local banner; banner=$("$1" --version 2>/dev/null) || return 1
    case "$(uname -m)" in
        arm64|aarch64) [[ "$banner" == *aarch64* || "$banner" == *arm64* ]] ;;
        x86_64|amd64)  [[ "$banner" == *x86_64* || "$banner" == *amd64* ]] ;;
        *)             return 0 ;;  # unfamiliar host arch: don't second-guess it
    esac
}

# Locate uv, installing a repo-local copy if the host has no usable one. uv is a
# static binary whose installer needs no Python — that is what makes it safe to
# depend on before any interpreter exists.
ensure_uv() {
    [ -n "${UV:-}" ] && [ -x "$UV" ] && return 0
    local found
    if found=$(command -v uv 2>/dev/null) && uv_arch_ok "$found"; then
        UV="$found"; return 0
    fi
    if [ -x "$REPO_ROOT/.uv/uv" ] && uv_arch_ok "$REPO_ROOT/.uv/uv"; then
        UV="$REPO_ROOT/.uv/uv"; return 0
    fi
    [ -n "${found:-}" ] && echo "Ignoring $found: built for a different architecture than $(uname -m)." >&2

    echo "Installing uv (Python toolchain manager)..."
    local script=""
    if command -v curl &>/dev/null; then
        script=$(curl -LsSf https://astral.sh/uv/install.sh) || script=""
    elif command -v wget &>/dev/null; then
        script=$(wget -qO- https://astral.sh/uv/install.sh) || script=""
    else
        echo "Neither curl nor wget is available; cannot fetch uv." >&2
        echo "Install uv manually (https://docs.astral.sh/uv/) and re-run." >&2
        return 1
    fi
    if [ -z "$script" ]; then
        echo "Could not download the uv installer (offline or blocked?)." >&2
        echo "Install uv manually (https://docs.astral.sh/uv/) and re-run." >&2
        return 1
    fi
    # Repo-local install: no writes to the user's home, no PATH mutation.
    printf '%s' "$script" | UV_INSTALL_DIR="$REPO_ROOT/.uv" UV_NO_MODIFY_PATH=1 sh >&2 || {
        echo "uv installation failed." >&2
        return 1
    }
    UV="$REPO_ROOT/.uv/uv"
    [ -x "$UV" ] || { echo "uv installer did not produce $UV" >&2; return 1; }
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

# Where dependency versions come from. requirements.lock is fully pinned and
# resolved for every supported platform, so two people installing months apart
# get the same tree; requirements.txt (open ranges) is the fallback for a
# checkout that predates the lock. Regenerate with scripts/update_lock.sh.
deps_source() {
    if [ -f "$REPO_ROOT/requirements.lock" ]; then
        echo "$REPO_ROOT/requirements.lock"
    else
        echo "$REPO_ROOT/requirements.txt"
    fi
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

# Is this venv the one .python-version asks for, on this machine's architecture?
# Guards against a venv left behind by an earlier setup — a different pin, or an
# interpreter that has since been upgraded or uninstalled out from under it.
venv_is_healthy() {
    local venv="$1"
    [ -x "$venv/bin/python" ] || return 1
    "$venv/bin/python" - "$(python_pin)" "$(uname -m)" <<'PY' 2>/dev/null
import platform, sys
want, arch = sys.argv[1], sys.argv[2]
have = ".".join(str(n) for n in sys.version_info[:2])
sys.exit(0 if have == want and platform.machine() == arch else 1)
PY
}

# --- Ensure venv exists, matches the pin, and is activated ---
# The venv is disposable, not precious: a warm rebuild takes under a second, so
# anything that doesn't match the pin is discarded rather than reused. Repairing
# in place is what let a mismatched venv survive run after run.
ensure_venv() {
    ensure_uv || return 1
    local venv="$REPO_ROOT/venv" pin; pin=$(python_pin)

    if [ -d "$venv" ] && ! venv_is_healthy "$venv"; then
        echo "Existing venv does not match Python $pin on $(uname -m); rebuilding..." >&2
        rm -rf "$venv"
    fi

    if [ ! -d "$venv" ]; then
        echo "Provisioning Python $pin..."
        "$UV" python install "$pin" >&2 || {
            echo "Could not provision Python $pin." >&2
            return 1
        }
        # --python-preference as a FLAG, not the UV_PYTHON_PREFERENCE env var:
        # the flag cannot be overridden by the user's environment, and without
        # it uv reuses any same-version interpreter it finds on PATH — which is
        # exactly the conda/Rosetta interpreter this design exists to avoid.
        "$UV" venv --python "$pin" --python-preference only-managed "$venv" >&2 || {
            echo "Failed to create the virtual environment." >&2
            return 1
        }
        # Assert what we just built, rather than assuming the toolchain got it
        # right. Without this, a wrong interpreter is not noticed until a
        # native package fails to build, minutes and many lines later.
        venv_is_healthy "$venv" || {
            echo "Provisioned interpreter is not Python $pin/$(uname -m):" >&2
            "$venv/bin/python" -c 'import platform,sys; print(" ", sys.version, platform.machine())' >&2 || true
            rm -rf "$venv"
            return 1
        }
    fi
    source "$venv/bin/activate"
}

# --- Install Python deps ---
# A failed install is fatal, not a warning: the installer applies dependencies
# as a single transaction, so one package that won't build leaves *nothing*
# installed, and carrying on defers the failure to whatever imports first.
install_python_deps() {
    ensure_uv || return 1
    local venv="$REPO_ROOT/venv"
    local src; src=$(deps_source)
    local log="$REPO_ROOT/logs/pip-install.log"
    mkdir -p "$REPO_ROOT/logs"

    echo "Installing Python dependencies from $(basename "$src") (log: $log)..."
    if ! "$UV" pip install --python "$venv/bin/python" -r "$src" >"$log" 2>&1; then
        echo "" >&2
        echo "Failed to install Python dependencies. Last 30 lines:" >&2
        tail -30 "$log" >&2
        echo "" >&2
        echo "Full log: $log" >&2
        return 1
    fi
    verify_python_env
}

# setup_dev.sh only. start.sh and the desktop path share install_python_deps and
# have no use for linters. Non-fatal: a missing linter should not block a dev env.
install_dev_deps() {
    ensure_uv || return 0
    local venv="$REPO_ROOT/venv"
    local log="$REPO_ROOT/logs/pip-install.log"
    mkdir -p "$REPO_ROOT/logs"

    echo "Installing lint toolchain from requirements-dev.txt..."
    if ! "$UV" pip install --python "$venv/bin/python" \
            -r "$REPO_ROOT/requirements-dev.txt" >>"$log" 2>&1; then
        echo "Warning: lint toolchain install failed (see $log) - continuing." >&2
        return 0
    fi
    # Announced, not silent: the next commit reformatting itself is otherwise
    # unexplained.
    if "$venv/bin/pre-commit" install >/dev/null 2>&1; then
        echo "Installed the pre-commit hook (black, isort, flake8 on services/ and core/)."
    else
        echo "Warning: could not install the pre-commit hook - continuing." >&2
    fi
}

# Prove the venv can import what the services actually need. Cause-agnostic by
# design: this catches a wrong-architecture interpreter, a half-finished
# install, a failed native build, or a venv orphaned by an OS upgrade — all of
# which otherwise surface far downstream as a bare ModuleNotFoundError from
# whichever module happened to import first.
verify_python_env() {
    local venv="$REPO_ROOT/venv"
    local probe='import sqlalchemy, fastapi, uvicorn, arq, redis, anthropic'
    "$venv/bin/python" -c "$probe" 2>/dev/null && return 0
    echo "" >&2
    echo "The venv is missing packages Vigil needs:" >&2
    "$venv/bin/python" -c "$probe" >&2 || true
    echo "" >&2
    echo "Install log: $REPO_ROOT/logs/pip-install.log" >&2
    echo "Delete venv/ and re-run to rebuild from scratch." >&2
    return 1
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
# startup. Mirrors REQUIRED_SERVICES in core/platform/service_manager.py.
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
