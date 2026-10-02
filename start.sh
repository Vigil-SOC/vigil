#!/bin/bash
# Start Vigil SOC
# Usage: ./start.sh [-d|--daemon] [--with <profile>] [--all]
source "$(dirname "$0")/scripts/lib.sh"

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

# Host State Directory, the path `backups.json` is read from.
host_state_dir() {
    local state="${VIGIL_DIR:-$HOME/.vigil}"
    mkdir -p "$state"
    (cd "$state" && pwd)
}

# Host paths and `docker compose run` flags every backup container shares: the
# create/restore one-shot, the pre-upgrade one-shot, and the schedule loop.
# Exports the State Directory and investigation workdir the compose `backup`
# service mounts and sets BACKUP_RUN_ARGS (host user, plus the repo-root .env
# when present, mounted $1: ro by default).
prepare_backup_mounts() {
    local env_mode="${1:-ro}"
    local investigations="${ORCHESTRATOR_WORKDIR:-$REPO_ROOT/data/investigations}"
    case "$investigations" in
        /*) ;;
        *) investigations="$REPO_ROOT/$investigations" ;;
    esac
    mkdir -p "$investigations"
    export VIGIL_BACKUP_STATE_DIR="$(host_state_dir)"
    export VIGIL_BACKUP_INVESTIGATIONS_DIR="$(cd "$investigations" && pwd)"
    BACKUP_RUN_ARGS=(--user "$(id -u):$(id -g)")
    if [ -f "$REPO_ROOT/.env" ]; then
        BACKUP_RUN_ARGS+=(-v "$REPO_ROOT/.env:/app/.env:$env_mode")
    fi
}

# Before the schema is touched: snapshot the default destination if this
# release's major.minor differs from the database's. Only when backups.json
# exists, so an install with no destination starts as before.
backup_pre_upgrade() {
    [ -f "$(host_state_dir)/$BACKUPS_FILE" ] || return 0
    prepare_backup_mounts
    # An old loop would hold the backup lock, and could snapshot mid-upgrade.
    docker rm -f "$BACKUP_LOOP_CONTAINER" >/dev/null 2>&1 || true
    local -a version_arg=()
    [ "$VERSION" = "dev" ] || version_arg=(--target-version "$VERSION")
    # --build: an image from before this command existed would exit 2.
    dc run --rm --build "${BACKUP_RUN_ARGS[@]}" backup-pre-upgrade ${version_arg[@]+"${version_arg[@]}"} || {
        echo "Pre-upgrade backup failed. Fix the destination in $BACKUPS_FILE," \
            "or set VIGIL_SKIP_PREUPGRADE_BACKUP=1 to start without one." >&2
        return 1
    }
}

# The schedule loop, detached, once the schema is ready. Replaces a loop left by
# an earlier start; shutdown_all.sh stops it by the same name.
start_backup_loop() {
    [ -f "$(host_state_dir)/$BACKUPS_FILE" ] || return 0
    prepare_backup_mounts
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
# Auth is on unless .env opts into DEV_MODE; the backend needs a signing secret.
ensure_jwt_secret || exit 1

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
backup_pre_upgrade || exit 1
python3 scripts/init_schema.py || { echo "Schema init failed."; exit 1; }
# Seed roles/reference data so first-run bootstrap can assign role-admin. No
# default admin is seeded — the empty user table triggers the bootstrap screen.
python3 scripts/seed_reference_data.py || true
start_backup_loop

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
