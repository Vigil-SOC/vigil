#!/usr/bin/env bash
# Starts the TypeScript agent layer against a stack already running locally.
#
# start.sh does not launch these, and nothing else drains the BullMQ queue the
# backend enqueues to: without them the console accepts a run, reports it queued
# and nothing ever picks it up. Same two commands and the same environment as
# infra/docker/docker-compose.yml's x-agent-env anchor, with host-side hosts.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

[ -f .env ] || { echo "no .env — copy env.example first" >&2; exit 1; }

# The backend checks this on every /internal call. Empty on either side answers
# 503, so a run fails before its first model call rather than at the seam.
AGENT_INTERNAL_TOKEN="$(sed -n 's/^AGENT_INTERNAL_TOKEN=//p' .env | head -1)"
if [ -z "$AGENT_INTERNAL_TOKEN" ]; then
    echo "AGENT_INTERNAL_TOKEN is unset in .env; every /internal call would answer 503" >&2
    exit 1
fi
export AGENT_INTERNAL_TOKEN
export VIGIL_TOOLS_TOKEN="$AGENT_INTERNAL_TOKEN"

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_DB="${POSTGRES_DB:-deeptempo_soc}"
export POSTGRES_USER="${POSTGRES_USER:-deeptempo}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-deeptempo_secure_password_change_me}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

# All four, because each defaults to this process rather than the backend.
export VIGIL_PLAYBOOKS_URL="${VIGIL_PLAYBOOKS_URL:-http://localhost:6987/internal/playbooks}"
export VIGIL_PRICING_URL="${VIGIL_PRICING_URL:-http://localhost:6987/internal/pricing}"
export VIGIL_RUNS_URL="${VIGIL_RUNS_URL:-http://localhost:6987/internal/runs}"
export VIGIL_TOOLS_URL="${VIGIL_TOOLS_URL:-http://localhost:6987/internal/tools/invoke}"
export BIFROST_URL="${BIFROST_URL:-http://localhost:8080}"
export VIGIL_ACTOR="${VIGIL_ACTOR:-$(whoami)}"

mkdir -p logs
cd services/agent
[ -d node_modules ] || npm install

start() {
    AGENT_HEALTH_PORT=6990 AGENT_HTTP_PORT=6989 \
        nohup npx tsx "$1.ts" > "$ROOT/logs/agent-$1.log" 2>&1 &
    echo $! > "$ROOT/logs/agent-$1.pid"
}
start worker
start serve

for port in 6990 6989; do
    for _ in $(seq 1 30); do
        curl -sf -m 2 "http://localhost:$port/healthz" >/dev/null 2>&1 && break
        sleep 1
    done
done

echo "agent worker  → logs/agent-worker.log  (health :6990)"
echo "agent serve   → logs/agent-serve.log   (health :6989)"
echo "stop with: kill \$(cat logs/agent-worker.pid logs/agent-serve.pid)"
