#!/bin/bash
# Start Vigil SOC
# Usage: ./start.sh [-d|--daemon] [--with <profile>] [--all]
source "$(dirname "$0")/scripts/lib.sh"

# Version shown in the startup banner, read from the repo VERSION file.
VERSION="$(cat "$(dirname "$0")/VERSION" 2>/dev/null || echo "dev")"

usage() {
    cat <<EOF
Usage: $0 [--daemon|-d] [--with <profile>] [--all]

  -d, --daemon      Run in the background (logs/ + pidfiles)
      --with NAME   Also start a profiled service (splunk, kafka, pgadmin,
                    jaeger, prometheus, grafana, otel-collector). Repeatable.
      --all         Also start every profiled service

Core services come from .vigil-autostart (or \$AUTOSTART_SERVICES, else
postgres redis bifrost ollama). --with/--all are additive to that list.
EOF
}

DAEMON=0
EXTRA_SERVICES=""
ALL_PROFILES=0
while [ $# -gt 0 ]; do
    case "$1" in
        -d|--daemon) DAEMON=1 ;;
        --all) ALL_PROFILES=1 ;;
        --with)
            [ -n "${2:-}" ] || { echo "--with requires a service name" >&2; exit 1; }
            EXTRA_SERVICES="$EXTRA_SERVICES $2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
    shift
done
[ "$ALL_PROFILES" -eq 1 ] && EXTRA_SERVICES="$EXTRA_SERVICES pgadmin splunk kafka jaeger prometheus grafana otel-collector"

# --- Prerequisites ---
ensure_docker || exit 1

SKIP_FRONTEND=0
# Opt out explicitly (e.g. to run scripts/agent_up.sh by hand) with SKIP_AGENT=1.
SKIP_AGENT="${SKIP_AGENT:-0}"
if ! command -v node &>/dev/null; then
    echo "Node.js not found. Frontend + agent layer will not start."; SKIP_FRONTEND=1; SKIP_AGENT=1
elif ! node -e "process.exit(parseInt(process.version.slice(1))>=18?0:1)" 2>/dev/null; then
    echo "Node.js 18+ required. Frontend + agent layer will not start."; SKIP_FRONTEND=1; SKIP_AGENT=1
fi

# --- Python environment ---
ensure_venv
install_python_deps

# --- Environment ---
_CALLER_BIND_HOST="${BIND_HOST:-}"
load_env
[ -n "$_CALLER_BIND_HOST" ] && BIND_HOST="$_CALLER_BIND_HOST"
export BIND_HOST="${BIND_HOST:-127.0.0.1}"

# `bifrost` only resolves inside the compose network. Rewrite before starting
# services: bringing Ollama up syncs its catalog into Bifrost, and that runs
# here on the host.
if [ -z "${BIFROST_URL+x}" ] || [ "${BIFROST_URL}" = "http://bifrost:8080" ]; then
    export BIFROST_URL="http://localhost:8080"
fi

# --- Services (autostart list + any --with/--all extras) ---
start_autostart_services
for svc in $EXTRA_SERVICES; do
    ensure_container "$(service_container "$svc")" "$svc" "$(service_profile "$svc")"
done

# --- Database init ---
python3 scripts/init_schema.py || { echo "Schema init failed."; exit 1; }
# Seed roles/reference data so first-run bootstrap can assign role-admin. No
# default admin is seeded — the empty user table triggers the bootstrap screen.
python3 scripts/seed_reference_data.py || true

# --- Frontend deps ---
if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web" ] && [ ! -d "clients/web/node_modules" ]; then
    (cd clients/web && npm install)
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
    if [ "${DEV_MODE:-}" = "true" ]; then
        echo "DEV_MODE active - auth bypassed"
    else
        echo "First run: create your admin account at http://localhost:6988"
    fi
    echo "=========================================="
}

start_frontend() {
    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web/node_modules" ]; then
        local host="$BIND_HOST"; [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
        wait_for_url "http://${host}:6987/api/health" 60 || true
        (cd clients/web && npm run dev) &
        FRONTEND_PID=$!
    fi
}

# The TypeScript agent layer drains the BullMQ agent-runs queue the backend
# enqueues to. Without it, a run is accepted, reported queued, and never picked
# up — no error anywhere. agent_up.sh self-backgrounds worker+serve, waits on
# their health, and writes logs/agent-{worker,serve}.pid; failures here are
# non-fatal so the rest of the stack still comes up.
start_agent_layer() {
    [ "$SKIP_AGENT" -eq 0 ] || return 0
    scripts/agent_up.sh || echo "Warning: agent layer failed to start (workflow runs won't be picked up)."
}

if [ "$DAEMON" -eq 0 ]; then
    # Foreground
    cleanup() {
        echo "Shutting down..."
        [ -n "${BACKEND_PID:-}" ] && kill $BACKEND_PID 2>/dev/null
        [ -n "${WORKER_PID:-}" ] && kill $WORKER_PID 2>/dev/null
        [ -n "${FRONTEND_PID:-}" ] && kill $FRONTEND_PID 2>/dev/null
        [ -f logs/agent-worker.pid ] && kill "$(cat logs/agent-worker.pid)" 2>/dev/null
        [ -f logs/agent-serve.pid ] && kill "$(cat logs/agent-serve.pid)" 2>/dev/null
        pkill -f "uvicorn services.api.main:app" 2>/dev/null
        exit 0
    }
    trap cleanup INT TERM EXIT

    uvicorn services.api.main:app --host "$BIND_HOST" --port 6987 --reload \
        --reload-dir services --reload-dir core --reload-dir tools &
    BACKEND_PID=$!

    python3 -m services.worker &
    WORKER_PID=$!

    start_frontend
    start_agent_layer
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
    [ "$(pgrep -f 'uvicorn services.api.main:app' | wc -l)" -gt 0 ] && {
        echo "Backend already running. Use ./shutdown_all.sh to stop."; exit 1;
    }

    nohup uvicorn services.api.main:app --host "$BIND_HOST" --port 6987 --reload \
        --reload-dir services --reload-dir core --reload-dir tools \
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

    nohup "${PWD}/venv/bin/python" services/daemon/main.py > logs/daemon.log 2>&1 &
    echo $! > logs/daemon.pid

    # Started unconditionally, independent of orchestrator.settings (#581).
    nohup "${PWD}/venv/bin/python" -m services.worker > logs/llm_worker.log 2>&1 &
    echo $! > logs/llm_worker.pid

    start_agent_layer

    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web/node_modules" ]; then
        # Absolute log dir: the `cd clients/web` only applies inside the
        # backgrounded (&) job, not the subsequent `echo`, which still runs
        # from the repo root — so a relative ../logs there pointed above the
        # repo and failed. Anchor both writes to the repo-root logs dir.
        logs_dir="${PWD}/logs"
        (cd clients/web && nohup npm run dev > "${logs_dir}/frontend.log" 2>&1 &
         echo $! > "${logs_dir}/frontend.pid")
    fi

    print_ready
    echo ""
    echo "Logs: tail -f logs/{backend,daemon,llm_worker,frontend,agent-worker,agent-serve}.log"
    echo "Stop: ./shutdown_all.sh"
fi
