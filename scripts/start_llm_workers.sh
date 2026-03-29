#!/bin/bash
# Start four ARQ LLM worker processes — one per priority queue.
#
# Priority order (highest → lowest):
#   triage        concurrency=LLM_MAX_CONCURRENT_TRIAGE        (default 3)
#   investigation concurrency=LLM_MAX_CONCURRENT_INVESTIGATION (default 2)
#   chat          concurrency=LLM_MAX_CONCURRENT_CHAT          (default 3)
#   insights      concurrency=LLM_MAX_CONCURRENT_INSIGHTS      (default 1)
#
# Usage:
#   ./scripts/start_llm_workers.sh            # foreground (Ctrl-C stops all)
#   ./scripts/start_llm_workers.sh --daemon   # background, logs to logs/llm_worker_*.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DAEMON=false

for arg in "$@"; do
    [[ "$arg" == "--daemon" ]] && DAEMON=true
done

# Activate virtualenv if present
if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
    source "$PROJECT_ROOT/venv/bin/activate"
fi

export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

mkdir -p "$PROJECT_ROOT/logs"

QUEUES=(
    "arq:llm:triage"
    "arq:llm:investigation"
    "arq:llm:chat"
    "arq:llm:insights"
)

PIDS=()

cleanup() {
    echo ""
    echo "Stopping LLM workers..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "All LLM workers stopped."
}

for queue in "${QUEUES[@]}"; do
    type="${queue##*:}"  # extract 'triage' from 'arq:llm:triage'
    log="$PROJECT_ROOT/logs/llm_worker_${type}.log"

    echo "Starting worker for queue: $queue"

    if $DAEMON; then
        LLM_WORKER_QUEUE="$queue" \
            python -m arq services.llm_worker.WorkerSettings \
            >> "$log" 2>&1 &
        PIDS+=($!)
    else
        LLM_WORKER_QUEUE="$queue" \
            python -m arq services.llm_worker.WorkerSettings \
            > >(sed "s/^/[$type] /") 2>&1 &
        PIDS+=($!)
    fi

done

if $DAEMON; then
    echo ""
    echo "LLM workers started in background."
    echo "Logs: logs/llm_worker_{triage,investigation,chat,insights}.log"
    echo "To stop: kill \$(pgrep -f 'arq services.llm_worker')"
else
    trap cleanup EXIT INT TERM
    echo ""
    echo "All LLM workers running. Press Ctrl-C to stop."
    wait
fi
