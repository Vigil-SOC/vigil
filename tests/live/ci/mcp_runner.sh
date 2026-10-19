#!/usr/bin/env bash
# Run the MCP runner on the job host, as the product runs it (#2308): as root,
# so each server gets its own provisioned uid via setpriv, with a fresh HOME and
# none of the job's env (no POSTGRES_*, no VIGIL_DIR, no vendor secrets): a
# server sees only what the probe process resolves and PUTs. Run from the
# checkout, with the interpreter that has requirements.lock installed.
#
# `up` provisions the per-server uids, starts the runner, waits for it, and
# appends MCP_RUNNER_URL / MCP_RUNNER_TOKEN to $LIVE_ENV_FILE. `down` stops it.
set -euo pipefail

STATE_DIR=/var/lib/vigil/mcp
RUNNER_HOME=/var/lib/vigil/runner-home
TOKEN_FILE=/var/lib/vigil/runner-token
URL=http://127.0.0.1:6991
LOG="${MCP_RUNNER_LOG:-$PWD/mcp-runner.log}"
ENV_FILE="${LIVE_ENV_FILE:-$(dirname "$0")/.env.runner}"
PY="$(command -v python)"
# Anchored, so it matches the runner and not the sudo that started it.
PROC="^$PY -m services.mcp_runner.main"

case "${1:-}" in
  up)
    sudo "$PY" -m services.mcp_runner.catalog "$STATE_DIR" >/dev/null
    # Server uids must reach the checkout (in-repo servers, cwd) and the
    # detection rules under $HOME: traverse-only on the dirs above them.
    for dir in "$PWD" "$HOME/security-detections"; do
      while [ "$dir" != / ]; do
        dir=$(dirname "$dir")
        if [ "$(stat -c %u "$dir")" = "$(id -u)" ]; then chmod o+x "$dir"; fi
      done
    done
    sudo setpriv --reuid=10001 --regid=10001 --clear-groups test -r "$PWD/mcp-config.json" ||
      { echo "server uids cannot read $PWD" >&2; exit 1; }

    TOKEN=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$TOKEN"
    sudo mkdir -p "$RUNNER_HOME"
    # A root-only file, not argv: sudo's command line is readable by every uid.
    sudo sh -c 'umask 077; cat >"$1"' sh "$TOKEN_FILE" <<<"$TOKEN"
    # Only these reach the runner. PATH carries setup-node/setup-uv for npx/uvx.
    ca=()
    for var in SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE; do
      [ -n "${!var:-}" ] && ca+=("$var=${!var}")
    done
    sudo env -i HOME="$RUNNER_HOME" PATH="$PATH" MCP_RUNNER_TOKEN_FILE="$TOKEN_FILE" ${ca[@]+"${ca[@]}"} \
      "$PY" -m services.mcp_runner.main >"$LOG" 2>&1 &

    for i in $(seq 1 60); do
      if curl -fsS -H "Authorization: Bearer $TOKEN" "$URL/servers" >/dev/null 2>&1; then
        echo "mcp runner ready after ~${i}s"
        {
          echo "MCP_RUNNER_URL=$URL"
          echo "MCP_RUNNER_TOKEN=$TOKEN"
        } >>"$ENV_FILE"
        exit 0
      fi
      sleep 1
    done
    echo "mcp runner did not answer GET /servers" >&2
    cat "$LOG" >&2
    exit 1
    ;;
  down)
    sudo pkill -TERM -f "$PROC" || true
    # It stops each server (stdin, then a grace period) before it exits.
    for _ in $(seq 1 30); do
      pgrep -f "$PROC" >/dev/null || break
      sleep 1
    done
    sudo pkill -KILL -f "$PROC" || true
    # The log carries every server's stderr and is uploaded as an artifact,
    # which Actions does not mask the way it masks the job log.
    if [ -f "$LOG" ]; then
      "$PY" - "$LOG" <<'EOF'
import os, re, sys
text = open(sys.argv[1], errors="replace").read()
for name, value in os.environ.items():
    if re.search(r"KEY|TOKEN|SECRET|PASSWORD", name) and len(value) >= 6:
        text = text.replace(value, "***")
open(sys.argv[1], "w").write(text)
EOF
    fi
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
