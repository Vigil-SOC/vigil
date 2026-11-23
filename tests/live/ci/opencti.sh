#!/usr/bin/env bash
# Run a local OpenCTI platform (opencti-compose.yml) for the opencti live check.
# `up` starts it with credentials made up for this run, waits until the GraphQL API
# answers with the admin token, seeds a small fixed set of intel (opencti_seed.py)
# and writes the env the connector and the probes need to $LIVE_ENV_FILE. `down`
# removes the platform, volumes included, and the env file.
#
# The mcp-config.json entry declares no env gate: the in-repo server reads
# OPENCTI_URL / OPENCTI_API_TOKEN through resolve(), like Settings would store them.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE=(docker compose -f "$HERE/opencti-compose.yml")
export OPENCTI_LIVE_PORT="${OPENCTI_LIVE_PORT:-18489}"
ENV_FILE="${LIVE_ENV_FILE:-$HERE/.env.opencti}"
URL="http://127.0.0.1:$OPENCTI_LIVE_PORT"

secret() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }

case "${1:-}" in
  up)
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
    : >"$ENV_FILE"
    OPENCTI_LIVE_ADMIN_TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$OPENCTI_LIVE_ADMIN_TOKEN"
    OPENCTI_LIVE_ENCRYPTION_KEY="$(python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')"
    OPENCTI_LIVE_ADMIN_PASSWORD="$(secret)"
    OPENCTI_LIVE_HEALTH_KEY="$(secret)"
    OPENCTI_LIVE_S3_USER="vigil$(secret | cut -c1-8)"
    OPENCTI_LIVE_S3_PASSWORD="$(secret)"
    OPENCTI_LIVE_AMQP_USER="vigil"
    OPENCTI_LIVE_AMQP_PASSWORD="$(secret)"
    export OPENCTI_LIVE_ADMIN_TOKEN OPENCTI_LIVE_ENCRYPTION_KEY OPENCTI_LIVE_ADMIN_PASSWORD \
      OPENCTI_LIVE_HEALTH_KEY OPENCTI_LIVE_S3_USER OPENCTI_LIVE_S3_PASSWORD \
      OPENCTI_LIVE_AMQP_USER OPENCTI_LIVE_AMQP_PASSWORD
    "${COMPOSE[@]}" up -d
    # The platform migrates its indices on first boot; ready = the token reads the version.
    for i in $(seq 1 120); do
      about=$(curl -fsS "$URL/graphql" -H "Authorization: Bearer $OPENCTI_LIVE_ADMIN_TOKEN" \
        -H 'Content-Type: application/json' -d '{"query":"{ about { version } }"}' 2>/dev/null) || about=""
      if grep -q '"version"' <<<"$about"; then
        echo "opencti ready after ~$((i * 5))s"
        {
          echo "OPENCTI_URL=$URL"
          echo "OPENCTI_API_TOKEN=$OPENCTI_LIVE_ADMIN_TOKEN"
          python3 "$HERE/opencti_seed.py" "$URL" "$OPENCTI_LIVE_ADMIN_TOKEN"
        } >>"$ENV_FILE"
        exit 0
      fi
      sleep 5
    done
    echo "opencti did not become ready" >&2
    "${COMPOSE[@]}" logs --tail 40 opencti >&2 || true
    exit 1
    ;;
  down)
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
