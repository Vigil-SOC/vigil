#!/bin/bash
# End-to-end: host-native Medic as a real vigil-medic user. For a throwaway
# machine with passwordless sudo (the CI runner, or a lab VM): it runs the setup
# commands medic.sh prints, which create a user, a sudo rule and two directories.
# Never run it on a machine you care about.
#
#   scripts/medic/host-native/tests/e2e_host_native.sh     (from the repo root)
#
# Covers privilege-model checks 8 and 12 and the host-native half of 9 (C3 §7),
# plus: Medic runs as vigil-medic from its own venv, `check` goes green, a kill -9
# is restarted after the backoff, and a TERM to the pidfile stops it all.
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
# Outside $HOME, whose own mode differs by OS: only the State Directory's mode decides.
STATE=$(mktemp -d)
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
[ -z "$out" ] && [ ! -e logs/medic.pid ] || fail "flag off printed or started something: $out"
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
grep -q "$STATE/master.key" <<<"$out" || fail "didn't name the readable secret: $out"
chmod 0700 "$STATE"; chmod 0600 "$STATE/master.key"
pass "readable secret refused (check 8)"

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

# Check 8, as the running user.
for f in master.key secrets.enc jwt_secret; do
    if sudo -n -u vigil-medic cat "$STATE/$f" >/dev/null 2>&1; then fail "vigil-medic can read $f"; fi
done
pass "vigil-medic can't read master.key, secrets.enc, jwt_secret (check 8)"

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
