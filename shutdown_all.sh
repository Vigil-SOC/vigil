#!/bin/bash
# Shutdown Vigil SOC processes
# Usage: ./shutdown_all.sh [-d|--docker] [--full]
source "$(dirname "$0")/scripts/lib.sh"

DOCKER_STOP=0; FULL=0
for arg in "$@"; do
    case "$arg" in
        -d|--docker) DOCKER_STOP=1 ;;
        --full) FULL=1 ;;
        *) echo "Usage: $0 [-d|--docker] [--full]"; exit 1 ;;
    esac
done

echo "Stopping Vigil SOC..."

# Kill by PID files
for pidfile in logs/backend.pid logs/daemon.pid logs/frontend.pid logs/llm_worker.pid; do
    [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null && rm -f "$pidfile"
done

# Kill by process pattern
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
pkill -f "daemon/main.py" 2>/dev/null || true
pkill -f "daemon.main" 2>/dev/null || true
pkill -f "vite.*opensoc" 2>/dev/null || true
pkill -f "mcp_servers.*_server" 2>/dev/null || true

# Kill by port
lsof -ti:6987 | xargs kill -9 2>/dev/null || true
lsof -ti:6988 | xargs kill -9 2>/dev/null || true

# Docker
if [ "$DOCKER_STOP" -eq 1 ]; then
    if command -v docker &>/dev/null; then
        if [ "$FULL" -eq 1 ]; then
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
