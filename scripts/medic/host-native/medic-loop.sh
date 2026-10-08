#!/bin/bash
# Medic's host-native restart loop (C5 ENG #3, option a). medic.sh copies this
# to <runtime>/bin/medic-loop and runs it as the vigil-medic user through the one
# sudo rule the setup instructions add, so it is also the only command that rule
# allows: hence the --probe and --check modes.
#
#   medic-loop --python P --app DIR --data-dir DIR [--agent-worker H:P] [tuning]   run Medic
#   medic-loop --python P --app DIR --data-dir DIR --check    `python -m services.medic check`
#   medic-loop --probe PATH...                                print the PATHs this user can read
#
# --agent-worker: where the agent worker's /readyz listens (default 127.0.0.1:6990;
# start.sh passes AGENT_HEALTH_PORT's). Medic always runs with install shape
# start_sh (L49).
# Tuning (C5 §5.2 defaults): --backoff-start 5 --backoff-max 300 (seconds, doubling
# per restart, back to the start after a run of --cap-window or longer);
# --cap-exits 5 --cap-window 600 (more exits than that in the window: give up and
# write `crash-looping` to the heartbeat file).
#
# Runs on macOS's bash 3.2: no associative arrays, no `wait -n`.
set -u

usage() { sed -n '7,9p' "$0" | sed 's/^# *//' >&2; exit 2; }
say() { printf 'medic-loop %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

PY="" APP="" DATA="" MODE=run WORKER=127.0.0.1:6990
START=5 MAX=300 CAP=5 WINDOW=600
[ $# -gt 0 ] || usage
while [ $# -gt 0 ]; do
    case "$1" in
        --probe) MODE=probe; shift; break ;;
        --check) MODE=check; shift ;;
        --python|--app|--data-dir|--agent-worker|--backoff-start|--backoff-max|--cap-exits|--cap-window)
            [ $# -ge 2 ] || usage
            case "$1" in
                --python) PY="$2" ;;
                --app) APP="$2" ;;
                --data-dir) DATA="$2" ;;
                --agent-worker) WORKER="$2" ;;
                --backoff-start) START="$2" ;;
                --backoff-max) MAX="$2" ;;
                --cap-exits) CAP="$2" ;;
                --cap-window) WINDOW="$2" ;;
            esac
            shift 2 ;;
        *) usage ;;
    esac
done

if [ "$MODE" = probe ]; then
    for p in "$@"; do
        [ -r "$p" ] && printf '%s\n' "$p"
    done
    exit 0
fi

if [ -z "$PY" ] || [ -z "$APP" ] || [ -z "$DATA" ]; then usage; fi
for n in "$START" "$MAX" "$CAP" "$WINDOW"; do
    case "$n" in ''|*[!0-9]*) usage ;; esac
done
# host:port, nothing else (Medic checks it again, app/config.py).
case "${WORKER%:*}" in ''|*[!A-Za-z0-9.-]*) usage ;; esac
case "${WORKER##*:}" in ''|*[!0-9]*) usage ;; esac

# Medic gets a fresh environment: only these, never the caller's. sudo resets the
# environment as well; this holds even where a sudoers rule keeps it (check 9).
# exec: run in the background, $! is then Medic's own PID, so TERM reaches it.
medic() {
    exec env -i PATH=/usr/bin:/bin HOME="$DATA" LANG=C.UTF-8 \
        PYTHONPATH="$APP" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        VIGIL_MEDIC_ENABLED=true VIGIL_MEDIC_DATA_DIR="$DATA" \
        VIGIL_MEDIC_INSTALL_SHAPE=start_sh VIGIL_MEDIC_AGENT_WORKER_ADDR="$WORKER" \
        "$PY" -m services.medic "$1"
}

cd "$APP" || exit 1
[ "$MODE" = check ] && medic check

umask 077

# Fail closed: ts 0 reads as stale, so `check` goes red at once.
mark_crash_looping() {
    local dir="$DATA/run" tmp
    mkdir -p "$dir" && chmod 700 "$dir" || return 0
    tmp="$dir/.heartbeat.loop.$$"
    printf '{"v": 1, "cycle": 0, "ts": 0, "started_at": 0, "pid": %d, "state": "crash-looping"}\n' \
        "$$" > "$tmp" && mv -f "$tmp" "$dir/heartbeat"
}

child="" stopping=0
on_stop() {
    stopping=1
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}
trap on_stop TERM INT HUP

exits=()
delay="$START"
while :; do
    started=$(date +%s)
    medic run &
    child=$!
    # TERM between `&` and `child=$!` found no child to pass on to.
    [ "$stopping" -eq 1 ] && kill -TERM "$child" 2>/dev/null
    say "started Medic (pid $child)"
    wait "$child"
    rc=$?
    # A trapped signal ends `wait` early; wait again for Medic's own exit.
    while kill -0 "$child" 2>/dev/null; do
        wait "$child"
        rc=$?
    done
    child=""
    if [ "$stopping" -eq 1 ]; then
        say "stopped"
        exit 0
    fi

    now=$(date +%s)
    ran=$((now - started))
    recent=()
    for t in ${exits[@]+"${exits[@]}"}; do
        [ $((now - t)) -lt "$WINDOW" ] && recent+=("$t")
    done
    exits=(${recent[@]+"${recent[@]}"} "$now")
    if [ "${#exits[@]}" -gt "$CAP" ]; then
        say "Medic exited (code $rc) ${#exits[@]} times in ${WINDOW}s; giving up (crash-looping)." \
            "Fix the cause in this log, then restart Vigil."
        mark_crash_looping
        exit 1
    fi
    [ "$ran" -ge "$WINDOW" ] && delay="$START"
    say "Medic exited (code $rc) after ${ran}s; restarting in ${delay}s"
    sleep "$delay" &
    sleeper=$!
    [ "$stopping" -eq 1 ] || wait "$sleeper"
    if [ "$stopping" -eq 1 ]; then
        kill "$sleeper" 2>/dev/null
        say "stopped"
        exit 0
    fi
    delay=$((delay * 2))
    [ "$delay" -gt "$MAX" ] && delay="$MAX"
done
