#!/usr/bin/env bash
# Run a local Cribl Stream leader (free Docker image) for the cribl-stream live
# check. `up` starts it, waits until the API answers a login, and appends the
# env the connector needs to $LIVE_ENV_FILE. `down` removes it.
#
# The mcp-config.json entry gates on CRIBL_BASE_URL / CRIBL_CLIENT_ID /
# CRIBL_CLIENT_SECRET (the cloud auth shape). The package's `local` mode reads
# CRIBL_USERNAME / CRIBL_PASSWORD instead, which the entry declares as optional
# (`${VAR:-}`, so they do not gate it), so the client id/secret below are
# placeholders that satisfy the gate and are never sent anywhere in local mode.
set -euo pipefail

NAME=vigil-live-cribl
PORT="${CRIBL_PORT:-9000}"
IMAGE="${CRIBL_IMAGE:-cribl/cribl:4.20.2}"
ENV_FILE="${LIVE_ENV_FILE:-$(dirname "$0")/.env.local}"
USER_NAME=admin
PASSWORD="${CRIBL_PASSWORD:-admin}"

case "${1:-}" in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # master mode: a leader with worker groups (the API the connector lists).
    docker run -d --name "$NAME" -p "$PORT:9000" -e CRIBL_DIST_MODE=master "$IMAGE" >/dev/null
    # Ready = a login works AND the config API serves the default group: the
    # login answers first, then "Config helper pool not initalized yet" for a while.
    for i in $(seq 1 90); do
      token=$(curl -fsS -X POST "http://localhost:$PORT/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$USER_NAME\",\"password\":\"$PASSWORD\"}" 2>/dev/null |
        sed -n 's/.*"token":"\([^"]*\)".*/\1/p') || token=""
      if [ -n "$token" ] && curl -fsS -H "Authorization: Bearer $token" \
        "http://localhost:$PORT/api/v1/m/default/pipelines" 2>/dev/null | grep -q '"items"'; then
        echo "cribl leader ready after ~$((i * 2))s"
        {
          echo "CRIBL_BASE_URL=http://localhost:$PORT"
          echo "CRIBL_AUTH_TYPE=local"
          echo "CRIBL_USERNAME=$USER_NAME"
          echo "CRIBL_PASSWORD=$PASSWORD"
          echo "CRIBL_CLIENT_ID=local-auth-unused"
          echo "CRIBL_CLIENT_SECRET=local-auth-unused"
        } >>"$ENV_FILE"
        exit 0
      fi
      sleep 2
    done
    echo "cribl did not become ready" >&2
    docker logs "$NAME" 2>&1 | tail -30 >&2
    exit 1
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
