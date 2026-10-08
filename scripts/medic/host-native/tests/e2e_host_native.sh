#!/bin/bash
# End-to-end: host-native Medic as a real vigil-medic user. For a throwaway
# machine with passwordless sudo (the CI runner, or a lab VM): it runs the setup
# commands medic.sh prints, which create a user, a sudo rule and two directories.
# Never run it on a machine you care about.
#
#   scripts/medic/host-native/tests/e2e_host_native.sh     (from the repo root)
#
# Covers privilege-model checks 8 and 12 and the host-native half of 9 (C3 §7),
# plus: DEV_MODE is refused (K1 T-37), Medic runs as vigil-medic from its own
# venv, reports install shape start_sh and reads the agent worker's /readyz on
# loopback (L49), the backend reads Medic's /v1/status on loopback (S9), `check`
# goes green, a kill -9 is restarted after the backoff,
# and a TERM to the pidfile stops it all.
set -uo pipefail

cd "$(dirname "$0")/../../../.." || exit 1
# shellcheck source=/dev/null
source scripts/lib.sh
# shellcheck source=/dev/null
source scripts/medic/host-native/medic.sh
set +e

fail() { echo "FAIL: $*" >&2; [ -f logs/medic.log ] && tail -n 30 logs/medic.log >&2; exit 1; }
pass() { echo "ok - $*"; }
wait_for() {  # seconds, command...
    local n="$1"; shift
    for _ in $(seq 1 "$n"); do "$@" && return 0; sleep 1; done
    return 1
}

sudo -n true || fail "needs passwordless sudo"
[ "$(id -un)" != root ] || fail "run as an ordinary user with sudo, not root"
id vigil-medic >/dev/null 2>&1 && fail "vigil-medic already exists; use a fresh machine"
mkdir -p logs
RT=/opt/vigil-medic
DATA=$(_medic_data_dir)
# In /tmp, not $HOME or macOS's per-user temp dir, whose own modes differ by OS:
# only the State Directory's mode should decide.
STATE=$(mktemp -d /tmp/vigil-e2e.XXXXXX)
export VIGIL_DIR="$STATE" JWT_SECRET_KEY=e2e-leak-me
for f in master.key secrets.enc jwt_secret; do
    (umask 077; echo "e2e-secret" > "$STATE/$f")
done
loop_check() {
    sudo -n -u vigil-medic -- "$RT/bin/medic-loop" --python "$RT/venv/bin/python" \
        --app "$RT/app" --data-dir "$DATA" --check >/dev/null 2>&1
}
medic_pid() { pgrep -u vigil-medic -f 'services.medic run' | head -n 1; }

# Check 12: off by default. Flag unset → silent, nothing started.
out=$(VIGIL_MEDIC_ENABLED='' medic_host_start 2>&1) || fail "flag off returned non-zero"
if [ -n "$out" ] || [ -e logs/medic.pid ]; then fail "flag off printed or started something: $out"; fi
pass "flag off: silent no-op (check 12)"

# Opted in with no user: the warning, then setup commands. Run exactly those.
export VIGIL_MEDIC_ENABLED=true
out=$(medic_host_start 2>&1) && fail "started without a vigil-medic user"
grep -q loopback <<<"$out" || fail "no loopback warning (check 12)"
pass "opted in: loopback warning shown (check 12)"
setup=$(awk '/^One-time setup/ {on=1; next} /^Or leave/ {on=0} on && /^  / {sub(/^  /, ""); print}' <<<"$out")
[ -n "$setup" ] || fail "no setup commands in: $out"
while IFS= read -r line; do printf '  + %s\n' "$line"; done <<<"$setup"
bash -euo pipefail -c "$setup" || fail "the printed setup commands failed"
pass "printed setup commands run as-is"

# Check 8: a secret Medic's user can read → refuses to start.
chmod 0755 "$STATE"; chmod 0644 "$STATE/master.key"
out=$(medic_host_start 2>&1) && fail "started although vigil-medic can read master.key"
grep -qxF "  chmod 0600 $STATE/master.key" <<<"$out" || fail "no exact chmod fix for the secret: $out"
chmod 0700 "$STATE"; chmod 0600 "$STATE/master.key"
pass "readable secret refused (check 8)"

# K1 T-37: DEV_MODE is refused, not warned (S8-7).
out=$(DEV_MODE=true medic_host_start 2>&1) && fail "started under DEV_MODE"
grep -q "DEV_MODE is on" <<<"$out" || fail "no DEV_MODE refusal: $out"
[ -e logs/medic.pid ] && fail "DEV_MODE refusal left a pidfile"
pass "DEV_MODE refused (T-37)"

# Start for real.
medic_host_start || fail "medic_host_start failed"
wait_for 60 loop_check || fail "check never went green"
pass "Medic runs and check is green"
pid=$(medic_pid)
[ -n "$pid" ] || fail "no Medic process owned by vigil-medic"
[ "$(ps -o uid= -p "$pid" | tr -d ' ')" = "$(id -u vigil-medic)" ] || fail "Medic isn't running as vigil-medic"
exe=$(ps -o args= -p "$pid")
case "$exe" in "$RT/venv/bin/python"*) ;; *) fail "Medic isn't on its own venv: $exe" ;; esac
pass "runs as vigil-medic from $RT/venv"
[ "$(_medic_stat "$DATA")" = "$(id -u vigil-medic) 700" ] || fail "$DATA isn't vigil-medic 0700"
pass "data dir $DATA is vigil-medic 0700"

# A stand-in agent worker /readyz on 127.0.0.1:6990, where scripts/agent_up.sh
# starts the real one (free on a CI runner); it records each path it serves.
# Started after Medic, on Medic's own venv (a bare python3 stalled on the macOS
# runner); Medic re-reads /readyz every 30 s.
READY_LOG="$STATE/readyz.hits"
"$RT/venv/bin/python" - "$READY_LOG" "$STATE/readyz.up" > "$STATE/readyz.err" 2>&1 <<'PY' &
import http.server, sys
hits, up = sys.argv[1], sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        open(hits, "a").write(self.path + "\n")
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 6990), H)
open(up, "w").write("up\n")
s.serve_forever()
PY
READY_PID=$!
trap 'kill "$READY_PID" 2>/dev/null' EXIT
ready_up() { [ -s "$STATE/readyz.up" ]; }
wait_for 30 ready_up || fail "the /readyz stand-in didn't start: $(cat "$STATE/readyz.err")"

# L49: install shape start_sh, and the worker reached on loopback.
read_ready() { grep -qx /readyz "$READY_LOG" 2>/dev/null; }
wait_for 90 read_ready || fail "Medic never read the agent worker's /readyz on 127.0.0.1:6990"
grep -q "shape start_sh" logs/medic.log || fail "Medic didn't report install shape start_sh"
pass "reports start_sh and reads /readyz on 127.0.0.1:6990 (L49)"

# Check 8, as the running user.
for f in master.key secrets.enc jwt_secret; do
    if sudo -n -u vigil-medic cat "$STATE/$f" >/dev/null 2>&1; then fail "vigil-medic can read $f"; fi
done
pass "vigil-medic can't read master.key, secrets.enc, jwt_secret (check 8)"

# S9: the backend's status path on this shape. No gateway: Vigil's user GETs
# Medic's /v1/status on loopback with the key file start.sh -d exports, exactly
# as the backend's poll does (core/platform/medic_last_seen.py fetch_status).
medic_host_backend_env || fail "medic_host_backend_env failed"
[ "$VIGIL_MEDIC_API_URL" = http://127.0.0.1:8470 ] || fail "backend URL: $VIGIL_MEDIC_API_URL"
[ "$VIGIL_MEDIC_API_KEY_FILE" = "$STATE/medic_api_key" ] || fail "key file: $VIGIL_MEDIC_API_KEY_FILE"
if sudo -n -u vigil-medic cat "$STATE/medic_api_key" >/dev/null 2>&1; then
    fail "vigil-medic can read Vigil's copy of the API key"
fi
backend_poll() {
    "$RT/venv/bin/python" - "$VIGIL_MEDIC_API_KEY_FILE" > "$STATE/status.out" 2>&1 <<'PY'
import http.client, json, sys
key = open(sys.argv[1]).read().strip()
def get(k):
    c = http.client.HTTPConnection("127.0.0.1", 8470, timeout=5)
    c.request("GET", "/v1/status", headers={"X-Medic-Key": k})
    r = c.getresponse()
    return r.status, r.read()
status, body = get(key)
assert status == 200, status
doc = json.loads(body)
assert doc["instance_id"].startswith("mi_") and doc["api_version"] == "1.0", doc
assert get("W" * 43)[0] == 401
print(doc["state"])
PY
}
wait_for 60 backend_poll || fail "backend-side poll of 127.0.0.1:8470 failed: $(cat "$STATE/status.out")"
pass "backend reads Medic's status on 127.0.0.1:8470 with the key (state $(cat "$STATE/status.out"))"
if command -v ss >/dev/null; then
    listen=$(ss -Hltn 'sport = :8470')
else
    listen=$(sudo -n lsof -nP -iTCP:8470 -sTCP:LISTEN)
fi
grep -q '127\.0\.0\.1' <<<"$listen" || fail "no loopback listener on 8470: $listen"
if grep -Eq '(\*|0\.0\.0\.0|\[::\]):8470' <<<"$listen"; then fail "8470 listens beyond loopback: $listen"; fi
pass "Medic's API listens on loopback only"
key=$(cat "$VIGIL_MEDIC_API_KEY_FILE")
if grep -qF "$key" logs/medic.log; then fail "the API key is in logs/medic.log"; fi
pass "the API key is in no log"

# Check 9, host-native half: no Vigil credential in Medic's environment.
if [ -r /proc/self/environ ]; then
    if sudo -n cat "/proc/$pid/environ" | tr '\0' '\n' | grep -q JWT_SECRET_KEY; then
        fail "JWT_SECRET_KEY reached Medic's environment"
    fi
else
    if sudo -n ps eww -p "$pid" | grep -q JWT_SECRET_KEY; then
        fail "JWT_SECRET_KEY reached Medic's environment"
    fi
fi
pass "Medic's environment holds no Vigil credential (check 9, host-native half)"

# Kill -9 → restarted after the 5 s backoff.
sudo -n kill -9 "$pid"
new_pid_differs() { local p; p=$(medic_pid); [ -n "$p" ] && [ "$p" != "$pid" ]; }
wait_for 30 new_pid_differs || fail "not restarted after kill -9"
grep -q "restarting in 5s" logs/medic.log || fail "no backoff line in logs/medic.log"
wait_for 60 loop_check || fail "check didn't go green after the restart"
pass "kill -9 → restarted after 5 s backoff, check green again"

# Stop the way shutdown_all.sh does: TERM to the pidfile's process.
kill "$(cat logs/medic.pid)"
gone() { ! pgrep -u vigil-medic >/dev/null; }
wait_for 20 gone || fail "vigil-medic processes left after TERM"
loop_check && fail "check still green after stop"
grep -q stopped logs/medic.log || fail "loop didn't log its stop"
pass "TERM to logs/medic.pid stops Medic cleanly; check red"
echo "All host-native e2e checks passed."
