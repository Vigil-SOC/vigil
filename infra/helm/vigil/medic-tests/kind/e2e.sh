#!/usr/bin/env bash
# Medic on a live kind cluster: C3 §7 checks 3, 14 (refuse variant) and 15, the
# S2-6 volume test, and the /readyz fault → chained, redacted incident.
#
#   e2e.sh calico    # a CNI that enforces NetworkPolicy: Medic must run
#   e2e.sh kindnet   # kind's default CNI ignores NetworkPolicy: Medic must refuse
#
# Needs docker, kind, kubectl, helm, and the two images built as
# vigil-medic:kind and vigil-medic-gateway:kind (the workflow builds them).
# CLUSTER names the kind cluster (default medic-<mode>); KEEP=1 leaves it up.
# Only stub pods stand in for Vigil: the backend and agent worker are tiny HTTP
# servers wearing the chart's labels, so the chart's own Services and policies
# select them as they would the real thing.
set -euo pipefail

MODE=${1:?usage: e2e.sh calico|kindnet}
CLUSTER=${CLUSTER:-medic-$MODE}
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
  $K -n "$NS" logs "deploy/$FN-medic" --previous --tail=40 2>/dev/null >&2 || true
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
if [[ $MODE == calico ]]; then
  printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnetworking:\n  disableDefaultCNI: true\n  podSubnet: 192.168.0.0/16\n' >"$cfg"
else
  printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n' >"$cfg"
fi
kind create cluster --name "$CLUSTER" --config "$cfg" --wait 0s
if [[ $MODE == calico ]]; then
  $K apply -f "https://raw.githubusercontent.com/projectcalico/calico/$CALICO/manifests/calico.yaml" >/dev/null
  $K -n kube-system rollout status ds/calico-node --timeout=300s
fi
$K wait --for=condition=Ready nodes --all --timeout=300s
kind load docker-image vigil-medic:kind vigil-medic-gateway:kind --name "$CLUSTER"

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
          image: vigil-medic:kind
          imagePullPolicy: Never
          command: ["python", "-c", $(printf '%s' "$STUB" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), "$3", "$4"]
          env: [{name: CANARY, value: "$CANARY"}]
          ports: [{containerPort: $3}]
EOF
}
stub stub-backend backend 6987 200
stub stub-agent-worker agent-worker 6990 503
$K -n "$NS" rollout status deploy/stub-backend deploy/stub-agent-worker --timeout=180s

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
helm --kube-context "kind-$CLUSTER" install "$REL" "$chart" -n "$NS" \
  --set medic.enabled=true \
  --set medic.image.repository=vigil-medic,medic.image.tag=kind,medic.image.pullPolicy=Never \
  --set medic.gateway.image.repository=vigil-medic-gateway,medic.gateway.image.tag=kind \
  --set medic.gateway.image.pullPolicy=Never \
  --set medic.gateway.viewer.username=medic-viewer \
  --set medic.gateway.viewer.passwordSecret.name=medic-viewer \
  --set networkPolicies.enabled=true \
  --set backend.replicaCount=0 --set agentWorker.replicaCount=0 \
  --set daemon.enabled=false --set llmWorker.enabled=false --set agentServe.enabled=false \
  --set dbInit.enabled=false \
  --set postgresql.enabled=false --set postgresql.external.host=none \
  --set redis.enabled=false --set redis.external.url=redis://none:6379/0 \
  --set secrets.postgresPassword=unused >/dev/null
api_cidrs=$($K -n "$NS" get networkpolicy "$FN-medic" -o jsonpath='{.spec.egress[*].to[*].ipBlock.cidr}')
api_ep=$($K get endpointslice kubernetes -n default -o jsonpath='{.endpoints[0].addresses[0]}')
api_port=$($K get endpointslice kubernetes -n default -o jsonpath='{.ports[0].port}')
[[ $api_cidrs == "$api_ep/32" ]] || fail "check 15: lookup filled kubeApi.cidrs=$api_cidrs, expected $api_ep/32"
ok "check 15: lookup filled medic.kubeApi.cidrs = $api_cidrs"

pod_exit() { $K -n "$NS" get pods -l app.kubernetes.io/component=medic \
  -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.exitCode}'; }

# --- kindnet: refuse to start (A3-3, check 14 refuse variant) --------------------
if [[ $MODE == kindnet ]]; then
  say "Medic must refuse to start: kindnet ignores NetworkPolicy"
  for _ in $(seq 60); do [[ -n $(pod_exit) ]] && break; sleep 3; done
  code=$(pod_exit)
  [[ $code == 3 ]] || fail "check 14: exit code ${code:-none}, expected 3"
  ok "check 14: Medic exited 3"
  logs=$($K -n "$NS" logs "deploy/$FN-medic" --previous 2>/dev/null || $K -n "$NS" logs "deploy/$FN-medic")
  grep -q "NetworkPolicy isn't enforced" <<<"$logs" || fail "check 14: no refusal message in: $logs"
  ok "check 14: message: $(grep -o "Medic refuses to run: NetworkPolicy isn't enforced[^.]*" <<<"$logs" | head -1)"
  [[ $($K -n "$NS" get pods -l app.kubernetes.io/component=medic -o jsonpath='{.items[0].status.containerStatuses[0].ready}') == false ]] \
    || fail "check 14: a refusing Medic reads Ready"
  ok "check 14: pod not Ready ($($K -n "$NS" get pods -l app.kubernetes.io/component=medic --no-headers | awk '{print $3}'))"
  say "PASS: $PASS checks ($MODE)"
  exit 0
fi

# --- calico: runs, isolated, records the fault -----------------------------------
say "Medic must run: Calico enforces NetworkPolicy"
$K -n "$NS" rollout status "deploy/$FN-medic" "deploy/$FN-medic-gateway" --timeout=300s \
  || fail "Medic or the gateway didn't become Ready"
[[ -z $(pod_exit) ]] || fail "Medic restarted (exit $(pod_exit))"
$K -n "$NS" logs "deploy/$FN-medic" | grep -q "NetworkPolicy is enforced" || fail "no enforced line"
ok "check 14: Medic Running and Ready; log: NetworkPolicy is enforced"

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
$K auth can-i --list --as="$SA" -n "$NS" | sed 's/^/        /'

say "isolation from inside Medic (R1, R2)"
gw_ip=$($K -n "$NS" get pod -l app.kubernetes.io/component=medic-gateway -o jsonpath='{.items[0].status.podIP}')
be_ip=$($K -n "$NS" get pod -l app.kubernetes.io/component=backend -o jsonpath='{.items[0].status.podIP}')
expect() { local got; got=$(reach "$1" "$2"); [[ $got == "$3" ]] || fail "$4: $1:$2 → $got, expected $3"; ok "$4: $1:$2 → $got"; }
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
sleep 5
expect "$api_ep" "$api_port" timeout "check 15 wrong cidrs"
helm --kube-context "kind-$CLUSTER" upgrade "$REL" "$chart" -n "$NS" --reuse-values \
  --set 'medic.kubeApi.cidrs=null' >/dev/null
$K -n "$NS" rollout status "deploy/$FN-medic" --timeout=300s >/dev/null

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
      image: vigil-medic:kind
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

say "PASS: $PASS checks ($MODE)"
