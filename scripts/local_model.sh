#!/usr/bin/env bash
# One local model Bifrost can route, on a machine with no provider key.
# Idempotent: a second run leaves a server that is already on the bridge alone.
#
# The Linux installer binds 127.0.0.1. Bifrost dials host.docker.internal, which
# is the docker bridge, and a localhost probe does not mean that path works.
# 0.0.0.0 would, and would also publish Ollama on the LAN; the compose stack
# keeps Bifrost on loopback for the same reason, so this listens on the bridge
# address only. Host-side catalog sync uses that same address: .env ships
# OLLAMA_URL=localhost, and the sync reads it.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

# qwen2.5 emits tool_calls on the wire the agent already sends. qwen3 does not
# unless thinking is off, and that flag is not this script's to add.
MODEL="${1:-${VIGIL_LOCAL_MODEL:-qwen2.5:1.5b}}"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running." >&2
  exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -qx deeptempo-bifrost; then
  echo "deeptempo-bifrost is not running. Start the compose stack first." >&2
  exit 1
fi

BRIDGE="$(docker exec deeptempo-bifrost cat /etc/hosts \
  | awk '/host\.docker\.internal$/ {print $1; exit}')"
case "$BRIDGE" in
  ""|0.0.0.0|127.0.0.1|localhost)
    echo "Bifrost has no usable host.docker.internal address (${BRIDGE:-empty})." >&2
    exit 1
    ;;
esac

# A loopback answer is the installer's default, and an answer on every
# interface is 0.0.0.0. Neither is the bridge.
on_bridge() {
  curl -sf --max-time 2 "http://${BRIDGE}:11434/api/tags" >/dev/null 2>&1 \
    && ! curl -sf --max-time 2 "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1
}

install_ollama() {
  if command -v ollama >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v zstd >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq
      sudo apt-get install -y zstd
    else
      echo "zstd is required to install Ollama." >&2
      exit 1
    fi
  fi
  curl -fsSL https://ollama.com/install.sh | sh
  hash -r
  command -v ollama >/dev/null 2>&1 || {
    echo "The Ollama installer did not put a binary on PATH." >&2
    exit 1
  }
}

systemd_up() {
  case "$(systemctl is-system-running 2>/dev/null || true)" in
    running|degraded) systemctl cat ollama.service >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

wait_for_bridge() {
  local _
  for _ in $(seq 30); do
    on_bridge && return 0
    sleep 1
  done
  echo "Ollama is not accepting connections on ${BRIDGE}:11434." >&2
  if systemd_up; then
    journalctl -u ollama -n 30 --no-pager >&2 || true
  else
    tail -n 30 "$ROOT/logs/ollama-local.log" >&2 || true
  fi
  return 1
}

# The server this script installed. An Ollama already answering on the bridge
# is left running; the supervisor is not asked to stop one it did not start.
ensure_serving() {
  if on_bridge; then
    echo "Ollama already listening on ${BRIDGE}:11434" >&2
    return 0
  fi
  install_ollama
  if systemd_up; then
    sudo mkdir -p /etc/systemd/system/ollama.service.d
    cat <<EOF | sudo tee /etc/systemd/system/ollama.service.d/listen.conf >/dev/null
[Service]
Environment="OLLAMA_HOST=${BRIDGE}:11434"
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable ollama >/dev/null
    sudo systemctl restart ollama
  else
    # No systemd: the installer writes the unit and does not start it.
    mkdir -p "$ROOT/logs"
    local old=""
    if [ -f "$ROOT/logs/ollama-local.pid" ]; then
      old="$(cat "$ROOT/logs/ollama-local.pid" 2>/dev/null || true)"
    fi
    # A recycled pid must not be signalled. Only the serve this script recorded.
    if [ -n "$old" ] && ps -p "$old" -o comm= 2>/dev/null | grep -qi ollama; then
      kill "$old" 2>/dev/null || true
      local _
      for _ in $(seq 20); do
        kill -0 "$old" 2>/dev/null || break
        sleep 0.25
      done
    fi
    OLLAMA_HOST="${BRIDGE}:11434" \
      setsid ollama serve >>"$ROOT/logs/ollama-local.log" 2>&1 </dev/null &
    echo "$!" >"$ROOT/logs/ollama-local.pid"
  fi
  wait_for_bridge
  echo "Ollama listening on ${BRIDGE}:11434" >&2
}

# Bifrost records list_models_failed while Ollama is down and does not retry
# until it reloads. The seeded key is what sync_after_ollama_start mirrors;
# a failed status leaves llm_provider_configs empty.
ensure_routable() {
  local base="${BIFROST_URL:-http://127.0.0.1:8080}"
  case "$base" in
    *://bifrost|*://bifrost:*) base="http://127.0.0.1:8080" ;;
  esac
  export BIFROST_URL="$base"

  key_status() {
    curl -sf --max-time 5 "${BIFROST_URL}/api/providers/ollama/keys" \
      | python3 -c 'import json,sys
d=json.load(sys.stdin)
ks=d.get("keys") or []
print((ks[0].get("status") or "") if ks else "")'
  }

  if [ "$(key_status 2>/dev/null || true)" = "success" ]; then
    echo "Bifrost ollama key already routable" >&2
    return 0
  fi
  echo "Restarting deeptempo-bifrost so it rechecks Ollama" >&2
  docker restart deeptempo-bifrost >/dev/null
  local _
  for _ in $(seq 40); do
    if curl -sf --max-time 2 "${BIFROST_URL}/health" >/dev/null 2>&1 \
      && [ "$(key_status 2>/dev/null || true)" = "success" ]; then
      echo "Bifrost ollama key routable" >&2
      return 0
    fi
    sleep 1
  done
  echo "Bifrost did not mark the ollama key routable." >&2
  return 1
}

resolve_model() {
  curl -sf --max-time 5 "http://${BRIDGE}:11434/api/tags" \
    | python3 -c 'import json,sys
want=sys.argv[1]
names=[m.get("name") for m in json.load(sys.stdin).get("models") or []]
if want in names:
    print(want)
elif ":" not in want and f"{want}:latest" in names:
    print(f"{want}:latest")
else:
    sys.exit(1)' "$MODEL"
}

mirror() {
  export OLLAMA_URL="http://${BRIDGE}:11434"
  local py="$ROOT/venv/bin/python"
  if [ ! -x "$py" ]; then
    echo "Project venv is missing ($py). Run ./start.sh once, then re-run this." >&2
    exit 1
  fi
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$py" - <<'PY'
import sys

from core.llm.bifrost.admin import sync_after_ollama_start
from core.storage.connection import get_db_manager
from core.storage.models import LLMProviderConfig

result = sync_after_ollama_start()
if not result.get("bifrost_synced"):
    print(f"sync did not finish: {result}", file=sys.stderr)
    sys.exit(1)

db = get_db_manager()
if db._engine is None:
    db.initialize()
with db.session_scope() as session:
    row = session.get(LLMProviderConfig, "bifrost-ollama")
    if row is None or not row.is_active:
        print(
            "sync finished but llm_provider_configs has no active ollama row",
            file=sys.stderr,
        )
        sys.exit(1)
PY
}

ensure_serving
# Pull progress stays off stdout so the only line there is the model id.
OLLAMA_HOST="${BRIDGE}:11434" ollama pull "$MODEL" >&2
NAME="$(resolve_model)"
ensure_routable
mirror
echo "model ${NAME} mirrored (address it by this id)" >&2
echo "$NAME"
