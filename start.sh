#!/bin/bash
# Start Vigil SOC
# Usage: ./start.sh [-d|--daemon] [--with <profile>] [--all]
source "$(dirname "$0")/scripts/lib.sh"
# Medic (System Watcher), host-native opt-in: a no-op unless VIGIL_MEDIC_ENABLED is on.
source "$(dirname "$0")/scripts/medic/host-native/medic.sh"

# Version shown in the startup banner, read from the repo VERSION file.
VERSION="$(cat "$(dirname "$0")/VERSION" 2>/dev/null || echo "dev")"
BACKUPS_FILE="backups.json"
BACKUP_LOOP_CONTAINER="vigil-backup-loop"

usage() {
    cat <<EOF
Usage: $0 [--daemon|-d] [--with <profile>] [--all]
       $0 backup --repo PATH --passphrase-file PATH
       $0 restore --repo PATH --passphrase-file PATH [--snapshot ID] [--test]

  -d, --daemon      Run in the background (logs/ + pidfiles)
      --with NAME   Also start a profiled service (splunk, kafka, pgadmin,
                    jaeger, prometheus, grafana, otel-collector). Repeatable.
      --all         Also start every profiled service
      backup        Run one snapshot in the backend image and exit. Does not
                    start the API, frontend, or agent layer.
      restore       Swap a snapshot in from the backup image and exit. Stop the
                    Bifrost container first. --test runs the checks and leaves
                    live state untouched. Pass JWT_SECRET_KEY in the shell when
                    Compose, not .env, holds it.

Core services come from .vigil-autostart (or \$AUTOSTART_SERVICES, else
postgres redis bifrost ollama). --with/--all are additive to that list.
EOF
}

# The schedule loop, detached, once the schema is ready. Replaces a loop left by
# an earlier start; shutdown_all.sh stops it by the same name.
start_backup_loop() {
    [ -f "$(host_state_dir)/$BACKUPS_FILE" ] || return 0
    prepare_backup_mounts
    save_container_logs "$BACKUP_LOOP_CONTAINER"
    docker rm -f "$BACKUP_LOOP_CONTAINER" >/dev/null 2>&1 || true
    dc run -d --no-deps --name "$BACKUP_LOOP_CONTAINER" "${BACKUP_RUN_ARGS[@]}" backup >/dev/null \
        || { echo "Warning: backup schedule failed to start." >&2; return 0; }
    # `compose run` starts it without a restart policy; give it the service's.
    docker update --restart unless-stopped "$BACKUP_LOOP_CONTAINER" >/dev/null || true
}

# One shot of the compose `backup` service, for `backup` and `restore`. That
# service's command is the schedule loop, so the entrypoint is overridden.
# Mounts the host State Directory, investigation workdir, and (when present)
# repo-root .env into that image. Skills and intent stay unset so a missing
# path is skipped. Sets BACKUP_RUN_ARGS for the caller to hand to `dc`.
#   $1 command (backup|restore)  $2 repo  $3 passphrase file  $4 .env mount mode
prepare_backup_run() {
    local cmd="$1" repo="$2" passfile="$3" env_mode="$4"
    [ -f "$passfile" ] || { echo "passphrase file not found: $passfile" >&2; exit 1; }
    ensure_docker || exit 1
    # Compose operators pass JWT_SECRET_KEY from their shell. It outranks .env,
    # and the .env sourced below must not turn into one.
    local caller_jwt="${JWT_SECRET_KEY:-}"
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$REPO_ROOT/.env"
        set +a
    fi
    if [ -n "$caller_jwt" ]; then
        export JWT_SECRET_KEY="$caller_jwt"
    else
        unset JWT_SECRET_KEY
    fi
    # A restore reads a repository that must already exist.
    if [ "$cmd" = "backup" ]; then
        mkdir -p "$repo"
    elif [ ! -d "$repo" ]; then
        echo "restore: repository not found: $repo" >&2
        exit 1
    fi
    repo="$(cd "$repo" && pwd)"
    passfile="$(cd "$(dirname "$passfile")" && pwd)/$(basename "$passfile")"
    export VIGIL_BACKUP_REPO="$repo"
    export VIGIL_BACKUP_PASSPHRASE_FILE="$passfile"
    prepare_backup_mounts "$env_mode"
    # `--rm` removes this one-shot; the service restart policy stays on `up`.
    BACKUP_RUN_ARGS=(run --rm "${BACKUP_RUN_ARGS[@]}")
    [ -n "$caller_jwt" ] && BACKUP_RUN_ARGS+=(-e JWT_SECRET_KEY)
    local sub="$cmd"
    [ "$cmd" = "backup" ] && sub="create"
    BACKUP_RUN_ARGS+=(--entrypoint python backup -m core.backup "$sub"
        --repo /backup/repo --passphrase-file /backup/passphrase
        --bifrost-data /var/lib/vigil/bifrost)
}

# Sets repo, passfile, snapshot and test from `backup`/`restore` arguments.
parse_backup_args() {
    local cmd="$1"; shift
    repo="" passfile="" snapshot="" test=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --repo)
                [ -n "${2:-}" ] || { echo "$cmd: --repo requires a path" >&2; exit 1; }
                repo="$2"; shift 2 ;;
            --passphrase-file)
                [ -n "${2:-}" ] || { echo "$cmd: --passphrase-file requires a path" >&2; exit 1; }
                passfile="$2"; shift 2 ;;
            --snapshot)
                [ "$cmd" = "restore" ] || { echo "Unknown argument: $1" >&2; usage >&2; exit 1; }
                [ -n "${2:-}" ] || { echo "$cmd: --snapshot requires an id" >&2; exit 1; }
                snapshot="$2"; shift 2 ;;
            --test)
                [ "$cmd" = "restore" ] || { echo "Unknown argument: $1" >&2; usage >&2; exit 1; }
                test=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
        esac
    done
    if [ -z "$repo" ] || [ -z "$passfile" ]; then
        echo "$cmd requires --repo and --passphrase-file" >&2
        usage >&2
        exit 1
    fi
}

run_backup() {
    local repo passfile snapshot test
    parse_backup_args backup "$@"
    prepare_backup_run backup "$repo" "$passfile" ro
    dc "${BACKUP_RUN_ARGS[@]}"
}

# Restore swaps the contents of each location, so it needs .env writable (the
# JWT rotation edits it) and Bifrost stopped (its SQLite files are replaced).
# --test leaves live state alone, so Bifrost may keep running for it.
run_restore() {
    local repo passfile snapshot test
    parse_backup_args restore "$@"
    if [ "$test" -eq 0 ]; then
        ensure_docker || exit 1
        if docker ps --format '{{.Names}}' | grep -qx "$(service_container bifrost)"; then
            echo "restore: stop $(service_container bifrost) first; restore replaces its data" >&2
            exit 1
        fi
    fi
    # A read-only repository is enough to check a snapshot.
    [ "$test" -eq 1 ] && export VIGIL_BACKUP_REPO_MODE=ro
    prepare_backup_run restore "$repo" "$passfile" rw
    [ -n "$snapshot" ] && BACKUP_RUN_ARGS+=(--snapshot "$snapshot")
    [ "$test" -eq 1 ] && BACKUP_RUN_ARGS+=(--test)
    dc "${BACKUP_RUN_ARGS[@]}"
}

case "${1:-}" in
    backup) shift; run_backup "$@"; exit $? ;;
    restore) shift; run_restore "$@"; exit $? ;;
esac

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
wsl_preflight
ensure_docker || exit 1

SKIP_FRONTEND=0
# Opt out explicitly (e.g. to run scripts/agent_up.sh by hand) with SKIP_AGENT=1.
SKIP_AGENT="${SKIP_AGENT:-0}"
if ! command -v node &>/dev/null; then
    echo "Node.js not found. Frontend + agent layer will not start."; SKIP_FRONTEND=1; SKIP_AGENT=1
elif ! node -e "process.exit(parseInt(process.version.slice(1))>=20?0:1)" 2>/dev/null; then
    echo "Node.js 20+ required. Frontend + agent layer will not start."; SKIP_FRONTEND=1; SKIP_AGENT=1
fi

# --- Python environment ---
ensure_venv
install_python_deps

# --- Environment ---
_CALLER_BIND_HOST="${BIND_HOST:-}"
load_env
[ -n "$_CALLER_BIND_HOST" ] && BIND_HOST="$_CALLER_BIND_HOST"
export BIND_HOST="${BIND_HOST:-127.0.0.1}"
# Daemon and agent listeners follow BIND_HOST unless the operator opts out.
export DAEMON_BIND_HOST="${DAEMON_BIND_HOST:-$BIND_HOST}"
export AGENT_BIND_HOST="${AGENT_BIND_HOST:-$BIND_HOST}"
# Auth is on unless .env opts into DEV_MODE; the backend needs a signing secret.
ensure_jwt_secret || exit 1
# And the agent layer needs its token, or agent_up.sh refuses to start. Covers
# an install that skipped setup_dev.sh.
ensure_agent_token
# Bifrost's management API refuses /api/* without this. Before compose starts
# the gateway: an empty value makes `up` fail rather than boot it open.
ensure_bifrost_admin_password

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

# --- Schema (upgrade one-shot; the API does not create tables) ---
run_schema_upgrade || exit 1
start_backup_loop

# --- Frontend deps ---
if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web" ] && [ ! -d "clients/web/node_modules" ]; then
    (cd clients/web && npm install)
fi

# --- Launch ---
export PYTHONPATH="${PWD}:${PYTHONPATH:-}"
LOGS_DIR="${PWD}/logs"
mkdir -p "$LOGS_DIR"

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
        echo "DEV_MODE active - auth bypassed (session auth, vstrike inbound)"
    else
        echo "First run: create your admin account at http://localhost:6988"
    fi
    echo "=========================================="
}

start_frontend() {
    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web/node_modules" ]; then
        local host="$BIND_HOST"; [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
        wait_for_url "http://${host}:6987/api/health" 60 || true
        # exec + vite directly: $! is Vite itself, not a subshell or npm (which
        # doesn't forward SIGTERM, leaving Vite orphaned on the port).
        (cd clients/web && exec node_modules/.bin/vite > >(tee -ia "$LOGS_DIR/frontend.log") 2>&1) &
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

# Outlasts the worker's own Redis connect retries (core/llm/gateway/gateway.py).
WORKER_READY_TIMEOUT=120

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
        exit "${STARTUP_FAILED:-0}"
    }
    trap cleanup INT TERM EXIT

    # Output goes to the terminal and the same logs/*.log files daemon mode
    # writes. tee -i survives Ctrl-C so shutdown output is still captured; piping
    # makes Python block-buffer, hence PYTHONUNBUFFERED.
    export PYTHONUNBUFFERED=1
    # Readable lines in a terminal; override with VIGIL_LOG_FORMAT=json.
    export VIGIL_LOG_FORMAT="${VIGIL_LOG_FORMAT:-text}"
    rotate_log "$LOGS_DIR/backend.log"
    rotate_log "$LOGS_DIR/llm_worker.log"
    rotate_log "$LOGS_DIR/frontend.log"

    uvicorn services.api.main:app --host "$BIND_HOST" --port 6987 --reload \
        --reload-dir services --reload-dir core --reload-dir tools \
        > >(tee -ia "$LOGS_DIR/backend.log") 2>&1 &
    BACKEND_PID=$!

    python3 -m services.worker > >(tee -ia "$LOGS_DIR/llm_worker.log") 2>&1 &
    WORKER_PID=$!
    # Logged from on_startup, i.e. once the worker has connected to Redis.
    wait_for_log_line "LLM worker" "$WORKER_PID" "$LOGS_DIR/llm_worker.log" \
        "LLM worker started" "$WORKER_READY_TIMEOUT" || { STARTUP_FAILED=1; exit 1; }

    start_frontend
    start_agent_layer
    medic_host_foreground_notice
    print_ready
    echo "Press Ctrl+C to stop"
    # The foreground is the local path and leaves the SOC daemon out (#902). Said
    # out loud, so an empty alert queue isn't read as a quiet day.
    echo "SOC daemon not started: no alert polling or auto-triage. ./start.sh -d runs it."

    # Open browser once frontend is ready
    if [ "$SKIP_FRONTEND" -eq 0 ]; then
        (sleep 3 && open_url "http://localhost:6988/") &
    fi

    wait
else
    # Daemon
    [ "$(pgrep -f 'uvicorn services.api.main:app' | wc -l)" -gt 0 ] && {
        echo "Backend already running. Use ./shutdown_all.sh to stop."; exit 1;
    }

    # Medic opted in: where the backend finds Medic's status (S9). Before uvicorn.
    medic_host_backend_env || true
    rotate_log logs/backend.log
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

    rotate_log logs/daemon.log
    nohup "${PWD}/venv/bin/python" services/daemon/main.py > logs/daemon.log 2>&1 &
    DAEMON_PID=$!
    echo $DAEMON_PID > logs/daemon.pid

    # Started unconditionally, independent of orchestrator.settings (#581).
    rotate_log logs/llm_worker.log
    nohup "${PWD}/venv/bin/python" -m services.worker > logs/llm_worker.log 2>&1 &
    WORKER_PID=$!
    echo $WORKER_PID > logs/llm_worker.pid

    # Both start in parallel; without these checks a crash at startup (e.g. Redis
    # not accepting connections yet) still ended in "Ready" (#2321).
    if ! wait_for_log_line "SOC daemon" "$DAEMON_PID" logs/daemon.log \
            "SOC Daemon fully operational" 60 \
        || ! wait_for_log_line "LLM worker" "$WORKER_PID" logs/llm_worker.log \
            "LLM worker started" "$WORKER_READY_TIMEOUT"; then
        echo "Startup incomplete; stop the rest with ./shutdown_all.sh" >&2
        exit 1
    fi

    start_agent_layer
    medic_host_start || true

    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "clients/web/node_modules" ]; then
        # Absolute log dir: the `cd clients/web` only applies inside the
        # backgrounded (&) job, not the subsequent `echo`, which still runs
        # from the repo root — so a relative ../logs there pointed above the
        # repo and failed. Anchor both writes to the repo-root logs dir.
        logs_dir="${PWD}/logs"
        rotate_log "${logs_dir}/frontend.log"
        # exec + vite directly (not `npm run dev`): the recorded PID is Vite
        # itself, so killing it can't orphan a child holding the port.
        (cd clients/web && exec nohup node_modules/.bin/vite > "${logs_dir}/frontend.log" 2>&1 &
         echo $! > "${logs_dir}/frontend.pid")
    fi

    print_ready
    echo ""
    echo "Logs: tail -f logs/{backend,daemon,llm_worker,frontend,agent-worker,agent-serve}.log"
    echo "Stop: ./shutdown_all.sh"
fi
