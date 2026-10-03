#!/bin/bash
# Shutdown Vigil SOC processes
# Usage: ./shutdown_all.sh [-d|--docker] [--full  DELETES ALL DATA]
source "$(dirname "$0")/scripts/lib.sh"

DOCKER_STOP=0; FULL=0
for arg in "$@"; do
    case "$arg" in
        -d|--docker) DOCKER_STOP=1 ;;
        --full) FULL=1 ;;
        -h|--help)
            cat <<'EOF'
--full with -d permanently deletes all Vigil data volumes. -d is the normal
way to stop everything and keeps all data. --full without -d does not delete
volumes.

Usage: ./shutdown_all.sh [-d|--docker] [--full  DELETES ALL DATA]

  -d, --docker   Stop containers and native processes. Keeps all data.
      --full     With -d, permanently delete all Vigil data volumes.
                 Without -d, volumes are not deleted.

down -v removes every named volume declared in the compose file, including
optional-profile volumes:

  postgres_data           database contents and settings (cases, findings, users)
  bifrost_data            Bifrost config and keys
  vigil_home              Compose State Directory (/home/vigil/.vigil),
                          including master.key (secrets.enc cannot be decrypted)
  vigil_investigations    investigation files
  redis_data              Redis
  backup_repo             default on-box backup repository

A backup has to be stored outside the compose volumes (a host path in
VIGIL_BACKUP_REPO, or a copy taken off the box) or this command deletes it too.
EOF
            exit 0
            ;;
        *) echo "Usage: $0 [-d|--docker] [--full  DELETES ALL DATA]"; exit 1 ;;
    esac
done

echo "Stopping Vigil SOC..."

# Kill by PID files
for pidfile in logs/backend.pid logs/daemon.pid logs/frontend.pid logs/llm_worker.pid \
               logs/agent-worker.pid logs/agent-serve.pid; do
    [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null && rm -f "$pidfile"
done

# Kill by process pattern.
# Deliberately no `pkill -f ollama`: the running Ollama is often the user's own
# (brew services / Ollama.app), so killing it destroys unrelated state and
# launchd just restarts it. Vigil starts Ollama but never stops it.
pkill -f "uvicorn services.api.main:app" 2>/dev/null || true
pkill -f "services/daemon/main.py" 2>/dev/null || true
pkill -f "services.daemon.main" 2>/dev/null || true
pkill -f 'services\.worker' 2>/dev/null || true
pkill -f "vite.*opensoc" 2>/dev/null || true
pkill -f "mcp_servers.*_server" 2>/dev/null || true

# Kill by port — only 6987/6988, which are unambiguously Vigil's. The agent
# serve (6989) and worker (6990) are already stopped by their pidfiles above; we
# deliberately do NOT port-kill those, because a user's Splunk UI can share 6990
# and a blind `kill -9` would take it down.
lsof -ti:6987 | xargs kill -9 2>/dev/null || true
lsof -ti:6988 | xargs kill -9 2>/dev/null || true

# The backup schedule loop start.sh launches runs in Docker but is Vigil's own
# process, so it stops with the rest whether or not -d is given.
if command -v docker &>/dev/null; then
    docker rm -f vigil-backup-loop >/dev/null 2>&1 || true
fi

# Docker
if [ "$DOCKER_STOP" -eq 1 ]; then
    if command -v docker &>/dev/null; then
        if [ "$FULL" -eq 1 ]; then
            echo "All Vigil data volumes are about to be deleted." >&2
            dc down -v || true
        else
            dc stop || true
        fi
    else
        echo "Docker not found; skipping container shutdown."
    fi
fi

# Status
echo ""
echo "Port 6987: $(lsof -ti:6987 2>/dev/null | wc -l | xargs) process(es)"
echo "Port 6988: $(lsof -ti:6988 2>/dev/null | wc -l | xargs) process(es)"
echo ""
[ "$DOCKER_STOP" -eq 0 ] && echo "Docker left running. Use -d to stop containers."
echo "Done."
