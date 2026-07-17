#!/usr/bin/env bash
# Bring the Vigil stack up for the desktop app and block until the backend is
# healthy, then exit 0. Unlike start.sh: no uvicorn --reload, no Vite dev server
# (the built SPA is served by the backend at :6987), backend is backgrounded
# with a pidfile so this script returns for the Electron main process to poll.
#
# Emits `STEP <phase> <status>` on stdout (start|ok|fail); detail on stderr.
source "$(dirname "$0")/lib.sh"

step() { echo "STEP $1 $2"; }

step env start
_CALLER_BIND_HOST="${BIND_HOST:-}"
load_env
[ -n "$_CALLER_BIND_HOST" ] && BIND_HOST="$_CALLER_BIND_HOST"
export BIND_HOST="${BIND_HOST:-127.0.0.1}"
# bifrost:8080 only resolves inside the compose network; host processes use
# localhost. Rewrite before starting services (Ollama sync talks to Bifrost).
if [ -z "${BIFROST_URL+x}" ] || [ "${BIFROST_URL}" = "http://bifrost:8080" ]; then
    export BIFROST_URL="http://localhost:8080"
fi
# The desktop app is a real-auth surface: its login and first-run bootstrap are
# the whole point. Force auth on regardless of the repo's .env (which defaults
# DEV_MODE=true for terminal dev) — for the backend, and baked into the SPA
# build below via VITE_DEV_MODE. Terminal start.sh keeps the .env value.
export DEV_MODE=false
export VITE_DEV_MODE=false

# DEV_MODE=false makes the backend fail closed without a JWT secret. Mint one
# once and persist it beside the encrypted secret store (regenerating would
# invalidate live sessions); respect one the caller — the desktop app — already
# set. umask keeps the file private.
if [ -z "${JWT_SECRET_KEY:-}" ]; then
    JWT_FILE="$HOME/.vigil/jwt_secret"
    if [ ! -s "$JWT_FILE" ]; then
        mkdir -p "$HOME/.vigil" && chmod 700 "$HOME/.vigil" 2>/dev/null || true
        (umask 177; { openssl rand -base64 48 2>/dev/null || head -c 48 /dev/urandom | base64; } \
            | tr -d '\n' > "$JWT_FILE")
    fi
    export JWT_SECRET_KEY="$(cat "$JWT_FILE")"
fi
step env ok

step docker start
ensure_docker >&2 || { step docker fail; exit 1; }
step docker ok

step services start
start_autostart_services >&2 || true
step services ok

# Use the venv binaries explicitly: the desktop app spawns this with a minimal
# GUI PATH, so we can't rely on an activated venv being on PATH.
VENV_PY="$REPO_ROOT/venv/bin/python"
VENV_UVICORN="$REPO_ROOT/venv/bin/uvicorn"
[ -x "$VENV_PY" ] || VENV_PY="$(command -v python3)"
[ -x "$VENV_UVICORN" ] || VENV_UVICORN="$(command -v uvicorn)"

# Rebuild the SPA if the source is newer than the built bundle. The backend
# serves frontend/build at :6987; without this, a stale build (e.g. from before
# the last pull) is shown in the window even though the source moved on.
step frontend start
REBUILT_FRONTEND=0
# Marker recording that build/ was produced in real-auth mode. Rebuild when it's
# missing so a bundle baked with DEV_MODE=true (which mocks a signed-in user and
# skips the login/bootstrap the desktop app exists to show) is replaced even
# when it is newer than src and the mtime check alone would pass it.
AUTH_MARKER="$REPO_ROOT/frontend/build/.vigil-real-auth"
if [ -d "$REPO_ROOT/frontend" ]; then
    if [ ! -f "$REPO_ROOT/frontend/build/index.html" ] || \
       [ ! -f "$AUTH_MARKER" ] || \
       [ -n "$(find "$REPO_ROOT/frontend/src" -type f -newer "$REPO_ROOT/frontend/build/index.html" -print -quit 2>/dev/null)" ]; then
        echo "SPA build is stale; rebuilding…" >&2
        ensure_npm_on_path || { step frontend fail; exit 1; }
        # Clean first so a prior build's orphaned chunks (e.g. a DEV_MODE=true
        # bundle) can't linger beside the fresh output.
        rm -rf "$REPO_ROOT/frontend/build"
        (cd "$REPO_ROOT/frontend" && npm run build) >&2 || { step frontend fail; exit 1; }
        touch "$AUTH_MARKER"
        REBUILT_FRONTEND=1
    fi
fi
step frontend ok

step schema start
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
"$VENV_PY" "$REPO_ROOT/scripts/init_schema.py" >&2 || { step schema fail; exit 1; }
# No default admin is seeded: the instance starts with an empty user table so
# the first-run bootstrap screen (POST /api/auth/bootstrap) fires, same as the
# packaged app. Keeps the two paths identical and out of default credentials.
step schema ok

step backend start
# The backend caches index.html in memory, so a reused backend would keep
# serving the old asset hashes after a rebuild (blank window). Restart it if we
# just rebuilt the SPA.
if [ "$REBUILT_FRONTEND" = "1" ] && [ -n "$(pgrep -f 'uvicorn backend.main:app')" ]; then
    echo "SPA rebuilt; restarting backend to serve the fresh bundle." >&2
    pkill -f 'uvicorn backend.main:app' 2>/dev/null || true
    sleep 2
fi
if [ -n "$(pgrep -f 'uvicorn backend.main:app')" ]; then
    echo "Backend already running; reusing it." >&2
else
    mkdir -p "$REPO_ROOT/logs"
    cd "$REPO_ROOT"
    # Fully detach the daemon: stdin from /dev/null and disown so this script
    # doesn't wait4() the backgrounded uvicorn and can return once it's healthy.
    nohup "$VENV_UVICORN" backend.main:app --host "$BIND_HOST" --port 6987 \
        </dev/null > "$REPO_ROOT/logs/backend.log" 2>&1 &
    BACKEND_PID=$!
    echo "$BACKEND_PID" > "$REPO_ROOT/logs/backend.pid"
    disown "$BACKEND_PID" 2>/dev/null || true
fi

health_host="$BIND_HOST"; [ "$health_host" = "0.0.0.0" ] && health_host="127.0.0.1"
if wait_for_url "http://${health_host}:6987/api/health" 90; then
    step backend ok
else
    echo "Backend did not become healthy; see logs/backend.log:" >&2
    tail -n 20 "$REPO_ROOT/logs/backend.log" >&2 2>/dev/null || true
    step backend fail
    exit 1
fi

echo "URL http://${health_host}:6987"
step up done
