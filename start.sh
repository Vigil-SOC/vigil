#!/bin/bash
# Start Vigil SOC
# Usage: ./start.sh [-d|--daemon]
source "$(dirname "$0")/scripts/lib.sh"

# Version shown in the startup banner, read from the repo VERSION file.
VERSION="$(cat "$(dirname "$0")/VERSION" 2>/dev/null || echo "dev")"

DAEMON=0
for arg in "$@"; do
    case "$arg" in
        -d|--daemon) DAEMON=1 ;;
        *) echo "Usage: $0 [--daemon|-d]"; exit 1 ;;
    esac
done

# --- Prerequisites ---
PYTHON=$(find_python)
command -v docker &>/dev/null || { echo "Docker required."; exit 1; }

SKIP_FRONTEND=0
if ! command -v node &>/dev/null; then
    echo "Node.js not found. Frontend will not start."; SKIP_FRONTEND=1
elif ! node -e "process.exit(parseInt(process.version.slice(1))>=18?0:1)" 2>/dev/null; then
    echo "Node.js 18+ required. Frontend will not start."; SKIP_FRONTEND=1
fi

# --- Git submodules ---
if [ -d ".git" ] && [ ! -f "deeptempo-core/pyproject.toml" ] && [ ! -f "deeptempo-core/setup.py" ]; then
    git submodule update --init --recursive || echo "Warning: submodule init failed."
fi

# --- Python environment ---
ensure_venv "$PYTHON"
install_python_deps

command -v uvicorn &>/dev/null || { echo "uvicorn not found after install."; exit 1; }

# --- Environment ---
_CALLER_BIND_HOST="${BIND_HOST:-}"
load_env
[ -n "$_CALLER_BIND_HOST" ] && BIND_HOST="$_CALLER_BIND_HOST"
export BIND_HOST="${BIND_HOST:-127.0.0.1}"

# --- Docker services ---
ensure_container deeptempo-postgres postgres
wait_for_postgres || true
ensure_container deeptempo-redis redis
ensure_container deeptempo-bifrost bifrost

if [ -z "${BIFROST_URL+x}" ] || [ "${BIFROST_URL}" = "http://bifrost:8080" ]; then
    export BIFROST_URL="http://localhost:8080"
fi

# --- Database init ---
python3 scripts/init_schema.py || { echo "Schema init failed."; exit 1; }
python3 scripts/init_default_user.py || true

# --- Frontend deps ---
if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend" ] && [ ! -d "frontend/node_modules" ]; then
    (cd frontend && npm install)
fi

# --- Launch ---
export PYTHONPATH="${PWD}:${PYTHONPATH:-}"

print_ready() {
    echo ""
    echo "=========================================="
    echo "Vigil SOC v$VERSION - Ready"
    echo "=========================================="
    echo "Backend:  http://localhost:6987"
    echo "Frontend: http://localhost:6988"
    echo "Docs:     http://localhost:6987/docs"
    echo ""
    echo "Login: admin / admin123 (change in production)"
    [ "${DEV_MODE:-}" = "true" ] && echo "DEV_MODE active - auth bypassed"
    echo "=========================================="
}

start_frontend() {
    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend/node_modules" ]; then
        local host="$BIND_HOST"; [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
        wait_for_url "http://${host}:6987/api/health" 60 || true
        (cd frontend && npm run dev) &
        FRONTEND_PID=$!
    fi
}

if [ "$DAEMON" -eq 0 ]; then
    # Foreground
    cleanup() {
        echo "Shutting down..."
        [ -n "${BACKEND_PID:-}" ] && kill $BACKEND_PID 2>/dev/null
        [ -n "${WORKER_PID:-}" ] && kill $WORKER_PID 2>/dev/null
        [ -n "${FRONTEND_PID:-}" ] && kill $FRONTEND_PID 2>/dev/null
        pkill -f "uvicorn backend.main:app" 2>/dev/null
        exit 0
    }
    trap cleanup INT TERM EXIT

    uvicorn backend.main:app --host "$BIND_HOST" --port 6987 --reload \
        --reload-dir backend --reload-dir services --reload-dir database &
    BACKEND_PID=$!

    python3 -m services.run_llm_worker &
    WORKER_PID=$!

    start_frontend
    print_ready
    echo "Press Ctrl+C to stop"

    # Open browser once frontend is ready
    if [ "$SKIP_FRONTEND" -eq 0 ]; then
        (sleep 3 && open "http://localhost:6988/" 2>/dev/null || xdg-open "http://localhost:6988/" 2>/dev/null) &
    fi

    wait
else
    # Daemon
    mkdir -p logs
    [ "$(pgrep -f 'uvicorn backend.main:app' | wc -l)" -gt 0 ] && {
        echo "Backend already running. Use ./shutdown_all.sh to stop."; exit 1;
    }

    nohup uvicorn backend.main:app --host "$BIND_HOST" --port 6987 --reload \
        --reload-dir backend --reload-dir services --reload-dir database \
        > logs/backend.log 2>&1 &
    BACKEND_PID=$!
    echo $BACKEND_PID > logs/backend.pid

    # Liveness check: bail if uvicorn died on startup or never serves health.
    local_host="$BIND_HOST"; [ "$local_host" = "0.0.0.0" ] && local_host="127.0.0.1"
    if ! wait_for_url "http://${local_host}:6987/api/health" 60 \
        || ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "Backend failed to start. See logs/backend.log:" >&2
        tail -n 20 logs/backend.log >&2 2>/dev/null || true
        exit 1
    fi

    nohup "${PWD}/venv/bin/python" daemon/main.py > logs/daemon.log 2>&1 &
    echo $! > logs/daemon.pid

    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend/node_modules" ]; then
        # Absolute log dir: the `cd frontend` only applies inside the
        # backgrounded (&) job, not the subsequent `echo`, which still runs
        # from the repo root — so a relative ../logs there pointed above the
        # repo and failed. Anchor both writes to the repo-root logs dir.
        logs_dir="${PWD}/logs"
        (cd frontend && nohup npm run dev > "${logs_dir}/frontend.log" 2>&1 &
         echo $! > "${logs_dir}/frontend.pid")
    fi

    print_ready
    echo ""
    echo "Logs: tail -f logs/{backend,daemon,frontend}.log"
    echo "Stop: ./shutdown_all.sh"
fi
