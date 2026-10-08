#!/usr/bin/env bash
# Medic on a live kind cluster: C3 §7 checks 3, 14 (refuse variant) and 15, the
# S2-6 volume test, and the /readyz fault → chained, redacted incident.
#
#   e2e.sh calico    # enforces NetworkPolicy: Medic must run (the full suite)
#   e2e.sh kindnet   # kind's default CNI, which enforces it too since kind
#                    # gained policy support: same suite as calico
#   e2e.sh nopolicy  # a bare ptp CNI with no policy controller: NetworkPolicy
#                    # is ignored, so Medic must refuse to start (exit 3)
#   e2e.sh backend   # Calico + the REAL backend (in-chart Postgres and Redis):
#                    # the backend reads Medic Running through the gateway with
#                    # the chart's key Secret, and Down <= 270 s after Medic
#                    # stops (V2-8). Local only: needs the backend image.
# calico also runs the REJECT leg: Calico set to reject denied traffic, and
# Medic must still read the policy as enforced (S7-2).
#
# Needs docker, kind, kubectl, helm, and the images built as vigil-medic:$TAG and
# vigil-medic-gateway:$TAG (TAG default kind; the workflow builds them), plus
# vigil-backend:$TAG for backend. CLUSTER names the kind cluster (default
# medic-<mode>); KEEP=1 leaves it up.
# Only stub pods stand in for Vigil: the backend and agent worker are tiny HTTP
# servers wearing the chart's labels, so the chart's own Services and policies
# select them as they would the real thing.
set -euo pipefail

MODE=${1:?usage: e2e.sh calico|kindnet|nopolicy|backend}
CLUSTER=${CLUSTER:-medic-$MODE}
TAG=${TAG:-kind}
NS=vigil
REL=rel
FN=$REL-vigil
CALICO=${CALICO_VERSION:-v3.30.3}
HERE=$(cd "$(dirname "$0")" && pwd)
CHART=$(cd "$HERE/../.." && pwd)
CANARY=medic-s7-canary-$RANDOM$RANDOM
K="kubectl --context kind-$CLUSTER"
PASS=0

say() { printf '\n== %s\n' "$*"; }
ok() { PASS=$((PASS + 1)); printf '   ok  %s\n' "$*"; }
fail() { printf '   FAIL %s\n' "$*" >&2; dump; exit 1; }
dump() {
  $K -n "$NS" get pods -o wide >&2 || true
  $K -n "$NS" logs "deploy/$FN-medic" --tail=40 >&2 || true
  $K -n "$NS" logs "deploy/$FN-medic" --previous --tail=40 >&2 2>/dev/null || true
}
cleanup() { [[ ${KEEP:-0} == 1 ]] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# In-pod TCP connect: prints connected | timeout | refused | <error>.
CONNECT='import socket,sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])).close(); print("connected")
except TimeoutError: print("timeout")
except ConnectionRefusedError: print("refused")
except OSError as e: print(type(e).__name__)'
reach() { $K -n "$NS" exec "deploy/$FN-medic" -- python -c "$CONNECT" "$1" "$2" "${3:-4}"; }

# --- cluster -------------------------------------------------------------------
say "kind cluster $CLUSTER ($MODE)"
cfg=$(mktemp)
case $MODE in
  calico | backend) printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnetworking:\n  disableDefaultCNI: true\n  podSubnet: 192.168.0.0/16\n' >"$cfg" ;;
  nopolicy) printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnetworking:\n  disableDefaultCNI: true\n  podSubnet: 10.244.0.0/16\n' >"$cfg" ;;
  kindnet) printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n' >"$cfg" ;;
  *) echo "unknown mode $MODE" >&2; exit 2 ;;
esac
kind create cluster --name "$CLUSTER" --config "$cfg" --wait 0s
if [[ $MODE == calico || $MODE == backend ]]; then
  $K apply -f "https://raw.githubusercontent.com/projectcalico/calico/$CALICO/manifests/calico.yaml" >/dev/null
  $K -n kube-system rollout status ds/calico-node --timeout=300s
fi
if [[ $MODE == nopolicy ]]; then
  # One node, the node image's own ptp + host-local plugins, and nothing that
  # reads NetworkPolicy objects: they are stored and ignored, as on flannel.
  docker exec -i "$CLUSTER-control-plane" sh -c 'cat > /etc/cni/net.d/10-ptp.conflist' <<'CNI'
{"cniVersion": "1.0.0", "name": "ptp", "plugins": [
  {"type": "ptp", "ipMasq": true, "ipam": {"type": "host-local",
   "ranges": [[{"subnet": "10.244.0.0/24"}]], "routes": [{"dst": "0.0.0.0/0"}]}},
  {"type": "portmap", "capabilities": {"portMappings": true}}]}
CNI
fi
$K wait --for=condition=Ready nodes --all --timeout=300s
kind load docker-image "vigil-medic:$TAG" "vigil-medic-gateway:$TAG" --name "$CLUSTER"
if [[ $MODE == backend ]]; then
  for img in postgres:16-alpine redis:7-alpine; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null
  done
  kind load docker-image "vigil-backend:$TAG" postgres:16-alpine redis:7-alpine --name "$CLUSTER"
fi

# --- stubs and secrets -----------------------------------------------------------
$K create namespace "$NS"
$K -n "$NS" create secret generic medic-viewer --from-literal=password="$(head -c 24 /dev/urandom | base64)"

# A tiny HTTP server in the Medic image (no other pull). /readyz answers 503 with
# a canary in the body and in a Set-Cookie header: neither may reach Medic's store
# or logs (K2).
STUB='import http.server,os,sys
port, status = int(sys.argv[1]), int(sys.argv[2]); canary = os.environ["CANARY"]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("not ready " + canary).encode() if status != 200 else b"{}"
        self.send_response(status); self.send_header("Set-Cookie", "session=" + canary)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("", port), H).serve_forever()'
stub() { # name component port status
  cat <<EOF | $K -n "$NS" apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata: {name: $1}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: vigil, app.kubernetes.io/instance: $REL, app.kubernetes.io/component: $2}}
  template:
    metadata: {labels: {app.kubernetes.io/name: vigil, app.kubernetes.io/instance: $REL, app.kubernetes.io/component: $2}}
    spec:
      containers:
        - name: stub
          image: vigil-medic:$TAG
          imagePullPolicy: Never
          command: ["python", "-c", $(printf '%s' "$STUB" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), "$3", "$4"]
          env: [{name: CANARY, value: "$CANARY"}]
          # Named like the real pods: the backend Service targets port "http".
          ports: [{name: http, containerPort: $3}]
EOF
}
stub stub-agent-worker agent-worker 6990 503
if [[ $MODE == backend ]]; then
  $K -n "$NS" rollout status deploy/stub-agent-worker --timeout=180s
else
  stub stub-backend backend 6987 200
  $K -n "$NS" rollout status deploy/stub-backend deploy/stub-agent-worker --timeout=180s
fi

# --- install -------------------------------------------------------------------
say "helm install (medic on, chart policies on, kubeApi.cidrs by lookup)"
chart=$(mktemp -d)/vigil
cp -R "$CHART" "$chart"
rm -rf "$chart/medic-tests" "$chart/Chart.lock"
# The subcharts are all off; strip them so no repo fetch is needed.
python3 - "$chart/Chart.yaml" <<'PY'
import re, sys
p = sys.argv[1]
text = open(p).read()
open(p, "w").write(re.split(r"^dependencies:", text, flags=re.M)[0])
PY
if [[ $MODE == backend ]]; then
  # The real backend, with the chart's own Postgres (+ db-init) and Redis.
  vigil_set=(--set backend.replicaCount=1
    --set backend.image.repository=vigil-backend --set backend.image.tag="$TAG"
    --set backend.image.pullPolicy=Never
    --set postgresql.persistence.enabled=false --set redis.persistence.enabled=false
    --set secrets.postgresPassword="$(head -c 18 /dev/urandom | base64 | tr -dc A-Za-z0-9)"
    --set secrets.jwtSecretKey="$(head -c 32 /dev/urandom | base64 | tr -dc A-Za-z0-9)")
else
  vigil_set=(--set backend.replicaCount=0 --set dbInit.enabled=false
    --set postgresql.enabled=false --set postgresql.external.host=none
    --set redis.enabled=false --set redis.external.url=redis://none:6379/0
    --set secrets.postgresPassword=unused)
fi
helm --kube-context "kind-$CLUSTER" install "$REL" "$chart" -n "$NS" \
  --set medic.enabled=true \
  --set medic.image.repository=vigil-medic,medic.image.tag="$TAG",medic.image.pullPolicy=Never \
  --set medic.gateway.image.repository=vigil-medic-gateway,medic.gateway.image.tag="$TAG" \
  --set medic.gateway.image.pullPolicy=Never \
  --set medic.gateway.viewer.username=medic-viewer \
  --set medic.gateway.viewer.passwordSecret.name=medic-viewer \
  --set networkPolicies.enabled=true \
  --set agentWorker.replicaCount=0 \
  --set daemon.enabled=false --set llmWorker.enabled=false --set agentServe.enabled=false \
  "${vigil_set[@]}" >/dev/null
api_cidrs=$($K -n "$NS" get networkpolicy "$FN-medic" -o jsonpath='{.spec.egress[*].to[*].ipBlock.cidr}')
api_ep=$($K get endpointslice kubernetes -n default -o jsonpath='{.endpoints[0].addresses[0]}')
api_port=$($K get endpointslice kubernetes -n default -o jsonpath='{.ports[0].port}')
[[ $api_cidrs == "$api_ep/32" ]] || fail "check 15: lookup filled kubeApi.cidrs=$api_cidrs, expected $api_ep/32"
ok "check 15: lookup filled medic.kubeApi.cidrs = $api_cidrs"

pod_exit() { $K -n "$NS" get pods -l app.kubernetes.io/component=medic \
  -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.exitCode}'; }

# --- kindnet: refuse to start (A3-3, check 14 refuse variant) --------------------
if [[ $MODE == nopolicy ]]; then
  say "Medic must refuse to start: nothing enforces NetworkPolicy here"
  for _ in $(seq 60); do [[ -n $(pod_exit) ]] && break; sleep 3; done
  code=$(pod_exit)
  [[ $code == 3 ]] || fail "check 14: exit code ${code:-none}, expected 3"
  ok "check 14: Medic exited 3"
  # The first start can beat kube-proxy to the new backend Service: refused, so
  # Medic refuses as "can't prove" (also exit 3). A later restart connects.
  for _ in $(seq 40); do
    logs=$($K -n "$NS" logs "deploy/$FN-medic" --previous 2>/dev/null || true)
    grep -q "NetworkPolicy isn't enforced" <<<"$logs" && break
    sleep 5
  done
  grep -q "NetworkPolicy isn't enforced" <<<"$logs" || fail "check 14: no refusal message in: $logs"
  ok "check 14: message: $(grep -o "Medic refuses to run: NetworkPolicy isn't enforced[^.]*" <<<"$logs" | head -1)"
  [[ $($K -n "$NS" get pods -l app.kubernetes.io/component=medic -o jsonpath='{.items[0].status.containerStatuses[0].ready}') == false ]] \
    || fail "check 14: a refusing Medic reads Ready"
  ok "check 14: pod not Ready ($($K -n "$NS" get pods -l app.kubernetes.io/component=medic --no-headers | awk '{print $3}'))"
  say "PASS: $PASS checks ($MODE)"
  exit 0
fi

# --- calico: runs, isolated, records the fault -----------------------------------
say "Medic must run: $MODE enforces NetworkPolicy"
$K -n "$NS" rollout status "deploy/$FN-medic" "deploy/$FN-medic-gateway" --timeout=300s \
  || fail "Medic or the gateway didn't become Ready"
# A brand-new pod can start before the CNI has programmed its policy (seen on
# kindnet): the probe then connects and Medic refuses, once, fail-closed. Allow
# that (exit 3 only), log what it said, and require the restart to run.
early=$(pod_exit)
if [[ -n $early ]]; then
  [[ $early == 3 ]] || fail "Medic crashed (exit $early)"
  first=$($K -n "$NS" logs "deploy/$FN-medic" --previous 2>/dev/null | grep -o "Medic refuses to run: [^(]*" | head -1 || true)
  printf '   note: an early start refused (exit 3): %s\n' "$first"
fi
$K -n "$NS" logs "deploy/$FN-medic" | grep -q "NetworkPolicy is enforced" || fail "no enforced line"
ok "check 14: Medic Running and Ready; log: NetworkPolicy is enforced"

# --- backend: the real backend reads Medic's status (V2-8, V2-2) -----------------
if [[ $MODE == backend ]]; then
  say "the real backend polls Medic through the gateway with the chart's key"
  $K -n "$NS" rollout status "deploy/$FN-backend" --timeout=600s || fail "backend not Ready"
  STATUS='from core.storage.connection import init_database
init_database(create_tables=False)
from core.platform.medic_last_seen import current_status
print(current_status().value)'
  status() { $K -n "$NS" exec "deploy/$FN-backend" -- python -c "$STATUS" 2>/dev/null | tail -1; }
  # shellcheck disable=SC2016 # expanded in the pod, not here
  key_mode=$($K -n "$NS" exec "deploy/$FN-backend" -- sh -c 'stat -L -c "%u:%g %a" "$VIGIL_MEDIC_API_KEY_FILE"')
  ok "backend's key file: $key_mode (secret volume, the pod's fsGroup)"
  got=""
  for _ in $(seq 60); do got=$(status || true); [[ $got == running ]] && break; sleep 5; done
  [[ $got == running ]] || fail "the backend reads Medic as '${got:-nothing}', not running"
  ok "backend reads Medic: running"
  $K -n "$NS" logs "deploy/$FN-medic-gateway" | grep -q '/v1/status' \
    || fail "the gateway's inbound log has no /v1/status"
  ok "the poll went backend → gateway inbound → Medic"
  say "kill Medic: Down within 270 s (V2-2)"
  $K -n "$NS" scale "deploy/$FN-medic" --replicas=0 >/dev/null
  t0=$(date +%s)
  for _ in $(seq 100); do got=$(status || true); [[ $got == down ]] && break; sleep 3; done
  took=$(( $(date +%s) - t0 ))
  [[ $got == down ]] || fail "still '${got:-nothing}' ${took}s after Medic stopped"
  (( took <= 270 )) || fail "Down took ${took}s (> 270 s)"
  ok "Down ${took}s after Medic stopped"
  $K -n "$NS" scale "deploy/$FN-medic" --replicas=1 >/dev/null
  for _ in $(seq 60); do got=$(status || true); [[ $got == running ]] && break; sleep 5; done
  [[ $got == running ]] || fail "after a restart the backend reads '${got:-nothing}'"
  ok "Medic back: running"
  say "PASS: $PASS checks ($MODE)"
  exit 0
fi

say "check 3: Medic's identity"
SA=system:serviceaccount:$NS:$FN-medic
# `can-i get pods/log` asks about a pod NAMED "log": subresources need the flag.
can() {
  local verb=$1 res=$2 sub=()
  if [[ $res == pods/* ]]; then sub=(--subresource="${res#pods/}"); res=pods; fi
  # ${sub[@]+...}: an empty array is "unbound" under set -u in bash 3.2 (macOS).
  $K auth can-i "$verb" "$res" ${sub[@]+"${sub[@]}"} --as="$SA" -n "$NS" 2>/dev/null || true
}
for v in "get pods" "list pods" "watch pods" "get pods/log" "list events" "get deployments.apps" "watch statefulsets.apps"; do
  [[ $(can $v) == yes ]] || fail "check 3: can't $v"
done
for v in "get secrets" "list configmaps" "create pods/exec" "create pods/portforward" "get pods/attach" \
  "delete pods" "patch deployments.apps" "create pods" "list pods/log" "get nodes"; do
  [[ $(can $v) == no ]] || fail "check 3: can $v"
done
[[ $($K auth can-i list pods --as="$SA" -n default 2>/dev/null || true) == no ]] || fail "check 3: reads another namespace"
ok "check 3: reads only (pods, pods/log get, events, deployments, statefulsets); no secrets, exec, writes, other namespaces"
# The whole list, not a sample: resource rows only, minus what every
# authenticated identity gets from the cluster's defaults (self-reviews, and
# clustertrustbundles since K8s 1.33's system:basic-user).
granted=$($K auth can-i --list --as="$SA" -n "$NS" 2>/dev/null \
  | awk 'NR > 1 && $1 !~ /^\[/ && match($0, /\[[^]]*\] *$/) {v = substr($0, RSTART); sub(/ *$/, "", v); print $1, v}' \
  | grep -v -E '^(selfsubject[a-z]*reviews\.|clustertrustbundles\.)' | sort)
expected=$(printf '%s\n' "deployments.apps [get list watch]" "events [get list watch]" \
  "pods [get list watch]" "pods/log [get]" "statefulsets.apps [get list watch]" | sort)
[[ $granted == "$expected" ]] || fail "check 3: can-i --list shows more than the Role: $granted"
ok "check 3: can-i --list = exactly the Role's 5 rows (+ cluster-default self-reviews)"

say "isolation from inside Medic (R1, R2)"
gw_ip=$($K -n "$NS" get pod -l app.kubernetes.io/component=medic-gateway -o jsonpath='{.items[0].status.podIP}')
be_ip=$($K -n "$NS" get pod -l app.kubernetes.io/component=backend -o jsonpath='{.items[0].status.podIP}')
expect() { local got; got=$(reach "$1" "$2") || got="exec failed"; [[ $got == "$3" ]] || fail "$4: $1:$2 → $got, expected $3"; ok "$4: $1:$2 → $got"; }
expect "$FN-backend" 6987 timeout "R1 backend Service"
expect "$be_ip" 6987 timeout "R1 backend pod"
expect "$gw_ip" 8471 connected "gateway outbound"
expect "$gw_ip" 8470 timeout "gateway inbound (backend's side)"
expect "$FN-medic-agent-worker" 6990 connected "agent worker /readyz"
expect 1.1.1.1 443 timeout "R2 internet"
expect "$api_ep" "$api_port" connected "check 15 Kubernetes API"

say "check 15: a wrong medic.kubeApi.cidrs cuts the API off"
helm --kube-context "kind-$CLUSTER" upgrade "$REL" "$chart" -n "$NS" --reuse-values \
  --set 'medic.kubeApi.cidrs[0]=10.255.255.1/32' >/dev/null
# Policy updates land asynchronously: poll rather than sleep.
await() { # host port want label
  local got
  for _ in $(seq 20); do
    got=$(reach "$1" "$2" 3) || got="exec failed"
    [[ $got == "$3" ]] && break
    sleep 1
  done
  [[ $got == "$3" ]] || fail "$4: $1:$2 → $got, expected $3"
  ok "$4: $1:$2 → $got"
}
await "$api_ep" "$api_port" timeout "check 15 wrong cidrs"
helm --kube-context "kind-$CLUSTER" upgrade "$REL" "$chart" -n "$NS" --reuse-values \
  --set 'medic.kubeApi.cidrs={}' >/dev/null
restored=$($K -n "$NS" get networkpolicy "$FN-medic" -o jsonpath='{.spec.egress[*].to[*].ipBlock.cidr}')
[[ $restored == "$api_ep/32" ]] || fail "check 15: an empty medic.kubeApi.cidrs left $restored, not the lookup's $api_ep/32"
await "$api_ep" "$api_port" connected "check 15 restored by lookup"

say "S2-6 / S7-1: the chart's volume (kind local-path = hostPath, which ignores fsGroup)"
root=$($K -n "$NS" exec "deploy/$FN-medic" -- stat -c '%u:%g %a' /var/lib/vigil-medic)
data=$($K -n "$NS" exec "deploy/$FN-medic" -- stat -c '%u:%g %a' /var/lib/vigil-medic/data)
ok "S7-1: volume root $root (fsGroup not applied); data/ $data; the store opened"

say "/readyz fault → chained, redacted incident (≥ 2 min of not-ready)"
INCIDENTS='import sqlite3, json
db = sqlite3.connect("file:/var/lib/vigil-medic/data/medic.db?mode=ro", uri=True)
cols = [r[1] for r in db.execute("pragma table_info(records)")]
for row in db.execute("select * from records order by seq"):
    rec = dict(zip(cols, row))
    print(json.dumps(rec, default=str))'
found=""
for _ in $(seq 80); do
  out=$($K -n "$NS" exec "deploy/$FN-medic" -- python -c "$INCIDENTS" 2>/dev/null || true)
  if grep -q incident_opened <<<"$out"; then found=1; break; fi
  sleep 5
done
[[ -n $found ]] || fail "no incident_opened after ~7 min: $out"
grep incident_opened <<<"$out" | head -1 | cut -c1-400 | sed 's/^/        /'
grep -q "pipeline.agent-worker-not-ready" <<<"$out" || fail "the incident isn't the /readyz rule"
ok "incident_opened for pipeline.agent-worker-not-ready"
$K -n "$NS" exec "deploy/$FN-medic" -- python -m services.medic.store verify \
  | sed 's/^/        /' || fail "chain doesn't verify"
ok "store verify: chain intact"
leak=$($K -n "$NS" exec "deploy/$FN-medic" -- sh -c "grep -rl '$CANARY' /var/lib/vigil-medic || true")
[[ -z $leak ]] || fail "canary in $leak"
$K -n "$NS" logs "deploy/$FN-medic" | grep -q "$CANARY" && fail "canary in Medic's log"
ok "canary (body + Set-Cookie) in neither the data dir nor the log"

say "S2-6: fresh volumes, with and without fsGroup, root vs data/"
# Opens the store at the volume root and at a data/ subdirectory Medic creates.
OPEN='from pathlib import Path
from services.medic.store import StoreError, open_writer
for d in (Path("/d"), Path("/d/data")):
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        open_writer(d).close()
        print(f"{d}: opened")
    except StoreError as e:
        print(f"{d}: refused: {e}")'
OPEN_JSON=$(printf '%s' "$OPEN" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
s26() { # name volumeType securityContext-extra
  cat <<EOF | $K -n "$NS" apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $1, annotations: {volumeType: $2}}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 100Mi}}}
---
apiVersion: v1
kind: Pod
metadata: {name: $1}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 10001, runAsGroup: 10001, runAsNonRoot: true $3}
  containers:
    - name: medic
      image: vigil-medic:$TAG
      imagePullPolicy: Never
      command: ["sh", "-c", "umask 077; stat -c '/d is %u:%g %a' /d; python -c \"\$OPEN\""]
      env: [{name: OPEN, value: $OPEN_JSON}]
      securityContext: {readOnlyRootFilesystem: true, allowPrivilegeEscalation: false}
      volumeMounts: [{name: d, mountPath: /d}]
  volumes: [{name: d, persistentVolumeClaim: {claimName: $1}}]
EOF
  $K -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$1" --timeout=120s >/dev/null \
    || fail "S2-6 pod $1 didn't finish"
  $K -n "$NS" logs "$1"
}
s26_case() { # name volumeType fsGroup(yes|no) expect-root expect-data
  local fsg="" out
  [[ $3 == yes ]] && fsg=", fsGroup: 10001, fsGroupChangePolicy: OnRootMismatch"
  out=$(s26 "$1" "$2" "$fsg")
  printf '%s\n' "$out" | sed 's/^/        /'
  grep -q "^/d: $4" <<<"$out" || fail "S2-6 $1: root not $4"
  grep -q "^/d/data: $5" <<<"$out" || fail "S2-6 $1: data/ not $5"
  ok "S2-6 $1 ($2, fsGroup $3): root $4, data/ $5"
}
s26_case s26-local-fsgroup local yes refused opened
s26_case s26-hostpath-nofsgroup hostPath no refused opened
s26_case s26-local-nofsgroup local no refused opened

# --- S7-2: a plugin that REJECTs denied traffic still counts as enforcing --------
if [[ $MODE == calico ]]; then
  say "S7-2: Calico set to Reject; Medic must still start"
  felix='{"spec":{"iptablesFilterDenyAction":"Reject","nftablesFilterDenyAction":"Reject"}}'
  $K patch felixconfiguration default --type merge -p "$felix" >/dev/null 2>&1 \
    || printf 'apiVersion: crd.projectcalico.org/v1\nkind: FelixConfiguration\nmetadata: {name: default}\nspec: {iptablesFilterDenyAction: Reject, nftablesFilterDenyAction: Reject}\n' \
      | $K apply -f - >/dev/null
  await "$gw_ip" 8470 refused "S7-2 gateway inbound under Reject"
  $K -n "$NS" rollout restart "deploy/$FN-medic" >/dev/null
  $K -n "$NS" rollout status "deploy/$FN-medic" --timeout=300s || fail "S7-2: Medic didn't come back under Reject"
  $K -n "$NS" logs "deploy/$FN-medic" | grep -q "NetworkPolicy is enforced: the probe to .* was refused every time" \
    || fail "S7-2: no 'refused every time' line"
  ok "S7-2: under Reject Medic logs 'refused every time' and is Ready"
fi

say "PASS: $PASS checks ($MODE)"
