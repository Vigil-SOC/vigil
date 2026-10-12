#!/usr/bin/env bash
# Start the self-hosted search backends the live connector checks run against,
# seeded with known data, and stop them again.
#
#   tests/live/ci/search_backends.sh up [elastic] [opensearch] [splunk]
#   tests/live/ci/search_backends.sh down
#
# `up` starts the named backends (all three by default) in parallel, waits for
# each to be healthy, seeds it, and writes the env the probes read as KEY=VALUE
# lines to $LIVE_ENV_FILE (default tests/live/ci/.env.search_backends). Load it
#   locally:   set -a && . "$LIVE_ENV_FILE" && set +a
#   in Actions: cat "$LIVE_ENV_FILE" >> "$GITHUB_ENV"
# A backend that fails to come up contributes no env (its probes skip as not
# configured), its container log is printed, and `up` exits non-zero.
#
# The passwords below are throwaway values for containers bound to loopback and
# deleted by `down`; they are not secrets.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_ENV_FILE="${LIVE_ENV_FILE:-$HERE/.env.search_backends}"

# Versions: Vigil's compose file runs Elasticsearch 8.15.0 and splunk/splunk;
# OpenSearch 2.x is the line its security-analytics findings API targets.
ELASTIC_IMAGE="${ELASTIC_IMAGE:-docker.elastic.co/elasticsearch/elasticsearch:8.15.0}"
OPENSEARCH_IMAGE="${OPENSEARCH_IMAGE:-opensearchproject/opensearch:2.17.1}"
SPLUNK_IMAGE="${SPLUNK_IMAGE:-splunk/splunk:10.0.11}"

ELASTIC_PORT="${LIVE_ELASTIC_PORT:-9200}"
OPENSEARCH_PORT="${LIVE_OPENSEARCH_PORT:-9201}"
SPLUNK_PORT="${LIVE_SPLUNK_PORT:-8089}"

PASSWORD="${LIVE_BACKEND_PASSWORD:-Vigil-Live-Ch3ck-9x}"
MARKER="vigil_live_7f3a9c"   # tests/live/test_search_backends.py searches for this
IOC_IP="203.0.113.77"        # TEST-NET-3, in 3 of the 5 seeded events

ELASTIC_NAME=vigil-live-elastic
OPENSEARCH_NAME=vigil-live-opensearch
SPLUNK_NAME=vigil-live-splunk
ELASTIC_INDEX=vigil-live-events
OPENSEARCH_INDEX=vigil-live-events
SPLUNK_INDEX=vigil_live

log() { printf '[%s] %s\n' "$1" "${*:2}"; }

# wait_for <label> <seconds> <command...>: poll until the command succeeds.
wait_for() {
  local label=$1 budget=$2 waited=0
  shift 2
  until "$@" >/dev/null 2>&1; do
    if [ "$waited" -ge "$budget" ]; then
      log "$label" "not ready after ${budget}s"
      return 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
  log "$label" "ready after ${waited}s"
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# The five seeded events, one JSON object per line. Three carry the marker and
# the IOC address; two are decoys with neither, so a search that matches
# everything (or nothing) is told apart from one that really filtered.
events() {
  local ts
  ts=$(now)
  cat <<EOF
{"@timestamp":"$ts","message":"login_failed user=svc-vigil src=$IOC_IP note=$MARKER","host":{"name":"vigil-live-host-a"},"source":{"ip":"$IOC_IP"},"user":{"name":"svc-vigil"},"event":{"action":"login_failed"}}
{"@timestamp":"$ts","message":"login_failed user=svc-vigil src=$IOC_IP note=$MARKER","host":{"name":"vigil-live-host-b"},"source":{"ip":"$IOC_IP"},"user":{"name":"svc-vigil"},"event":{"action":"login_failed"}}
{"@timestamp":"$ts","message":"process_start image=powershell.exe note=$MARKER","host":{"name":"vigil-live-host-a"},"source":{"ip":"198.51.100.5"},"user":{"name":"alice"},"event":{"action":"process_start"}}
{"@timestamp":"$ts","message":"login_ok user=alice src=198.51.100.5","host":{"name":"vigil-live-host-c"},"source":{"ip":"198.51.100.5"},"user":{"name":"alice"},"event":{"action":"login_ok"}}
{"@timestamp":"$ts","message":"dns_query name=example.org","host":{"name":"vigil-live-host-c"},"source":{"ip":"198.51.100.6"},"user":{"name":"bob"},"event":{"action":"dns_query"}}
EOF
}

# bulk_body <index>: the events as an _bulk request body.
bulk_body() {
  events | while IFS= read -r line; do
    printf '{"index":{"_index":"%s"}}\n%s\n' "$1" "$line"
  done
}

# ---------------------------------------------------------------- Elasticsearch

up_elastic() {
  local url="http://127.0.0.1:$ELASTIC_PORT"
  docker rm -f "$ELASTIC_NAME" >/dev/null 2>&1 || true
  log elastic "starting $ELASTIC_IMAGE (security on, basic auth over http)"
  docker run -d --name "$ELASTIC_NAME" \
    -p "127.0.0.1:$ELASTIC_PORT:9200" \
    -e discovery.type=single-node \
    -e xpack.security.enabled=true \
    -e xpack.security.http.ssl.enabled=false \
    -e xpack.ml.enabled=false \
    -e cluster.routing.allocation.disk.threshold_enabled=false \
    -e ELASTIC_PASSWORD="$PASSWORD" \
    -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
    "$ELASTIC_IMAGE" >/dev/null

  wait_for elastic 240 curl -fsS -u "elastic:$PASSWORD" \
    "$url/_cluster/health?wait_for_status=yellow&timeout=2s"

  bulk_body "$ELASTIC_INDEX" | curl -fsS -u "elastic:$PASSWORD" \
    -H 'Content-Type: application/x-ndjson' -X POST \
    "$url/_bulk?refresh=wait_for" --data-binary @- >/tmp/vigil-live-elastic-bulk.json
  if grep -q '"errors":true' /tmp/vigil-live-elastic-bulk.json; then
    log elastic "bulk seed reported errors: $(cat /tmp/vigil-live-elastic-bulk.json)"
    return 1
  fi
  local n
  n=$(curl -fsS -u "elastic:$PASSWORD" "$url/$ELASTIC_INDEX/_count" | sed -E 's/.*"count":([0-9]+).*/\1/')
  [ "$n" = 5 ] || { log elastic "expected 5 seeded events, found $n"; return 1; }
  log elastic "seeded $n events into $ELASTIC_INDEX"

  cat >"$LIVE_ENV_FILE.elastic" <<EOF
ELASTIC_SIEM_ELASTICSEARCH_URL=$url
ELASTIC_SIEM_USERNAME=elastic
ELASTIC_SIEM_PASSWORD=$PASSWORD
ELASTIC_SIEM_INDEX_PATTERN=vigil-live-*
EOF
}

# ------------------------------------------------------------------- OpenSearch

up_opensearch() {
  local url="https://127.0.0.1:$OPENSEARCH_PORT"
  docker rm -f "$OPENSEARCH_NAME" >/dev/null 2>&1 || true
  log opensearch "starting $OPENSEARCH_IMAGE (security plugin on, basic auth over self-signed https)"
  docker run -d --name "$OPENSEARCH_NAME" \
    -p "127.0.0.1:$OPENSEARCH_PORT:9200" \
    --ulimit nofile=65536:65536 --ulimit memlock=-1:-1 \
    -e discovery.type=single-node \
    -e bootstrap.memory_lock=false \
    -e OPENSEARCH_INITIAL_ADMIN_PASSWORD="$PASSWORD" \
    -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
    "$OPENSEARCH_IMAGE" >/dev/null

  wait_for opensearch 240 curl -fsSk -u "admin:$PASSWORD" \
    "$url/_cluster/health?wait_for_status=yellow&timeout=2s"

  bulk_body "$OPENSEARCH_INDEX" | curl -fsSk -u "admin:$PASSWORD" \
    -H 'Content-Type: application/x-ndjson' -X POST \
    "$url/_bulk?refresh=wait_for" --data-binary @- >/tmp/vigil-live-opensearch-bulk.json
  if grep -q '"errors":true' /tmp/vigil-live-opensearch-bulk.json; then
    log opensearch "bulk seed reported errors: $(cat /tmp/vigil-live-opensearch-bulk.json)"
    return 1
  fi
  local n
  n=$(curl -fsSk -u "admin:$PASSWORD" "$url/$OPENSEARCH_INDEX/_count" | sed -E 's/.*"count":([0-9]+).*/\1/')
  [ "$n" = 5 ] || { log opensearch "expected 5 seeded events, found $n"; return 1; }
  log opensearch "seeded $n events into $OPENSEARCH_INDEX"

  # The security plugin's demo certificate is self-signed, so verification is
  # off for this client only; the credential path is still exercised.
  cat >"$LIVE_ENV_FILE.opensearch" <<EOF
OPENSEARCH_OPENSEARCH_URL=$url
OPENSEARCH_USERNAME=admin
OPENSEARCH_PASSWORD=$PASSWORD
OPENSEARCH_VERIFY_SSL=false
EOF
}

# ----------------------------------------------------------------------- Splunk

up_splunk() {
  local url="https://127.0.0.1:$SPLUNK_PORT"
  docker rm -f "$SPLUNK_NAME" >/dev/null 2>&1 || true
  log splunk "starting $SPLUNK_IMAGE (linux/amd64; minutes to provision, emulated on Apple Silicon)"
  docker run -d --name "$SPLUNK_NAME" --platform linux/amd64 \
    -p "127.0.0.1:$SPLUNK_PORT:8089" \
    -e SPLUNK_START_ARGS=--accept-license \
    -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
    -e SPLUNK_PASSWORD="$PASSWORD" \
    "$SPLUNK_IMAGE" >/dev/null

  splunk_healthy() {
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$SPLUNK_NAME")" = healthy ] &&
      curl -fsSk -u "admin:$PASSWORD" "$url/services/server/info?output_mode=json"
  }
  wait_for splunk 1200 splunk_healthy

  # A dedicated index, so the seed is not mixed with whatever `main` holds.
  curl -fsSk -u "admin:$PASSWORD" "$url/services/data/indexes" \
    -d "name=$SPLUNK_INDEX" -d output_mode=json >/dev/null

  # Raw events through the receivers endpoint, one POST each so every event
  # carries its own host. Same five events as the other backends, as key=value
  # text rather than JSON.
  splunk_event() { # host, raw (the raw event is the body; the rest is query string)
    curl -fsSk -u "admin:$PASSWORD" -X POST --data-binary "$2" \
      "$url/services/receivers/simple?index=$SPLUNK_INDEX&host=$1&sourcetype=vigil%3Alive&source=vigil-live" >/dev/null
  }
  splunk_event vigil-live-host-a "action=login_failed user=svc-vigil src=$IOC_IP note=$MARKER"
  splunk_event vigil-live-host-b "action=login_failed user=svc-vigil src=$IOC_IP note=$MARKER"
  splunk_event vigil-live-host-a "action=process_start image=powershell.exe note=$MARKER"
  splunk_event vigil-live-host-c "action=login_ok user=alice src=198.51.100.5"
  splunk_event vigil-live-host-c "action=dns_query name=example.org"

  # Indexing is asynchronous: wait until a search sees all five.
  splunk_count() {
    local out
    out=$(curl -fsSk -u "admin:$PASSWORD" "$url/services/search/jobs/export" \
      --data-urlencode "search=search index=$SPLUNK_INDEX | stats count" \
      -d earliest_time=0 -d output_mode=csv)
    [ "$(printf '%s' "$out" | tail -n1 | tr -d '"\r')" = 5 ]
  }
  wait_for splunk-seed 180 splunk_count
  log splunk "seeded 5 events into $SPLUNK_INDEX"

  cat >"$LIVE_ENV_FILE.splunk" <<EOF
SPLUNK_URL=$url
SPLUNK_USERNAME=admin
SPLUNK_PASSWORD=$PASSWORD
SPLUNK_VERIFY_SSL=false
EOF
}

# ------------------------------------------------------------------------- main

up() {
  local which=("$@")
  [ ${#which[@]} -gt 0 ] || which=(elastic opensearch splunk)
  command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
  rm -f "$LIVE_ENV_FILE" "$LIVE_ENV_FILE".elastic "$LIVE_ENV_FILE".opensearch "$LIVE_ENV_FILE".splunk
  mkdir -p "$(dirname "$LIVE_ENV_FILE")"

  local pids=() b started
  started=$(date +%s)
  for b in "${which[@]}"; do
    case "$b" in elastic | opensearch | splunk) ;; *) echo "unknown backend: $b" >&2; exit 2 ;; esac
    "up_$b" &
    pids+=($!)
  done

  local failed=0 i=0
  for b in "${which[@]}"; do
    if ! wait "${pids[$i]}"; then
      failed=1
      echo "::error::$b did not come up" >&2
      docker logs --tail 60 "vigil-live-$b" >&2 2>&1 || true
    fi
    i=$((i + 1))
  done

  : >"$LIVE_ENV_FILE"
  for b in "${which[@]}"; do
    if [ -f "$LIVE_ENV_FILE.$b" ]; then
      cat "$LIVE_ENV_FILE.$b" >>"$LIVE_ENV_FILE"
      rm -f "$LIVE_ENV_FILE.$b"
    fi
  done
  log up "env written to $LIVE_ENV_FILE ($(($(date +%s) - started))s total)"
  return $failed
}

down() {
  docker rm -f -v "$ELASTIC_NAME" "$OPENSEARCH_NAME" "$SPLUNK_NAME" >/dev/null 2>&1 || true
  rm -f "$LIVE_ENV_FILE" "$LIVE_ENV_FILE".elastic "$LIVE_ENV_FILE".opensearch "$LIVE_ENV_FILE".splunk
  log down "removed search backend containers"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) down ;;
  *) echo "usage: $0 up [elastic|opensearch|splunk ...] | down" >&2; exit 2 ;;
esac
