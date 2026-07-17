#!/usr/bin/env bash
# Stop the Vigil app processes on desktop-app quit. Thin wrapper over
# shutdown_all.sh: by default stops backend/daemon/worker but leaves Docker
# containers and Ollama running (fast relaunch; Ollama may be the user's own).
# Pass --stop-docker to also stop the compose containers.
source "$(dirname "$0")/lib.sh"

STOP_DOCKER=0
for arg in "$@"; do
    case "$arg" in
        --stop-docker) STOP_DOCKER=1 ;;
        *) echo "Usage: $0 [--stop-docker]" >&2; exit 1 ;;
    esac
done

if [ "$STOP_DOCKER" -eq 1 ]; then
    "$REPO_ROOT/shutdown_all.sh" --docker
else
    "$REPO_ROOT/shutdown_all.sh"
fi
