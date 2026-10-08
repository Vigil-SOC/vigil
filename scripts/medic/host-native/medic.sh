# shellcheck shell=bash
# Medic (System Watcher) on a host-native install. Source after scripts/lib.sh;
# start.sh calls medic_host_start in daemon mode and medic_host_foreground_notice
# in the foreground. Off by default (C8, C3 §4.7): unless VIGIL_MEDIC_ENABLED is
# on, nothing here prints, writes or starts anything.
#
# Opted in, Medic runs as its own OS user (vigil-medic, A1), from its own venv
# built from services/medic/uv.lock, under medic-loop (restart with backoff, C5),
# with its data in /var/lib/vigil-medic (macOS: /Library/Application
# Support/vigil-medic). Nothing here creates a user, a directory owned by
# another user, or a sudo rule: when one is missing it prints the commands and
# leaves Medic off. Vigil starts either way.
#
# Settings: VIGIL_MEDIC_ENABLED; VIGIL_MEDIC_DATA_DIR; VIGIL_MEDIC_HOST_RUNTIME
# (default /opt/vigil-medic): the copy of Medic's code, its venv and interpreter
# and the loop, readable by vigil-medic and writable only by Vigil's user. Not in
# the repo: vigil-medic can't, and mustn't, read the checkout (.env, ~/Documents).

_MEDIC_USER=vigil-medic

# The same values Medic's own config treats as on (services/medic/app/config.py).
medic_host_enabled() {
    local v
    v=$(printf '%s' "${VIGIL_MEDIC_ENABLED:-}" | tr '[:upper:]' '[:lower:]' \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    case "$v" in true|1|yes|on) return 0 ;; esac
    return 1
}

medic_host_foreground_notice() {
    medic_host_enabled || return 0
    echo "Medic: not started. On a host-native install it runs only under" \
        "./start.sh -d, which restarts it when it exits." >&2
}

_medic_os() { printf '%s' "${MEDIC_HOST_OS:-$(uname -s)}"; }

_medic_data_dir() {
    if [ -n "${VIGIL_MEDIC_DATA_DIR:-}" ]; then
        printf '%s' "$VIGIL_MEDIC_DATA_DIR"
    elif [ "$(_medic_os)" = Darwin ]; then
        printf '%s' "/Library/Application Support/vigil-medic"
    else
        printf '%s' /var/lib/vigil-medic
    fi
}

# "<uid> <mode>": GNU stat, else BSD.
_medic_stat() { stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1" 2>/dev/null; }

# K1 T-14 (and T-37 under DEV_MODE). Shown every time Medic is opted in.
_medic_warning() {
    cat >&2 <<EOF
WARNING: Medic (System Watcher) is on for this host-native install. Here it
shares this machine's network: it can reach the backend, Redis and Bifrost on
loopback, so it doesn't meet Vigil's rule that Medic has no route to anything
that can change Vigil's state (K1 T-14). Its own OS user keeps it away from
Vigil's secret files, not from those services. For an isolated Medic, use
Compose (--profile medic) or Helm.
EOF
    if [ "${DEV_MODE:-}" = "true" ]; then
        echo "WARNING: DEV_MODE is on, so every backend request is an admin with no" \
            "login. Don't run Medic on this install (K1 T-37)." >&2
    fi
}

# A UID/GID below 500 (hidden on macOS) that no user or group has yet.
_medic_free_id() {
    local used id
    used=$({ dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID; } 2>/dev/null \
        | awk '{print $2}') || used=""
    [ -n "$used" ] || { printf '<a free id below 500>'; return; }
    for id in $(seq 499 -1 400); do
        printf '%s\n' "$used" | grep -qx "$id" || { printf '%s' "$id"; return; }
    done
    printf '<a free id below 500>'
}

# Why Medic isn't starting, and the exact one-time commands that fix it.
_medic_setup() {
    local me data runtime id
    me=$(id -un)
    data=$(printf '%q' "$(_medic_data_dir)")
    runtime="${VIGIL_MEDIC_HOST_RUNTIME:-/opt/vigil-medic}"
    {
        echo "Medic not started: $1."
        echo "One-time setup, as an admin (each line is safe to re-run), then ./start.sh -d again:"
        if [ "$(_medic_os)" = Darwin ]; then
            id=$(_medic_free_id)
            echo "  sudo dscl . -create /Groups/$_MEDIC_USER PrimaryGroupID $id"
            echo "  sudo dscl . -create /Users/$_MEDIC_USER UniqueID $id"
            echo "  sudo dscl . -create /Users/$_MEDIC_USER PrimaryGroupID $id"
            echo "  sudo dscl . -create /Users/$_MEDIC_USER UserShell /usr/bin/false"
            echo "  sudo dscl . -create /Users/$_MEDIC_USER NFSHomeDirectory /var/empty"
            echo "  sudo dscl . -create /Users/$_MEDIC_USER IsHidden 1"
        else
            echo "  id $_MEDIC_USER >/dev/null 2>&1 || sudo useradd --system --user-group" \
                "--no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin $_MEDIC_USER"
        fi
        echo "  sudo install -d -o $_MEDIC_USER -g $_MEDIC_USER -m 0700 $data"
        echo "  sudo install -d -o $me -m 0755 $(printf '%q' "$runtime")"
        echo "  echo '$me ALL=($_MEDIC_USER) NOPASSWD: $runtime/bin/medic-loop' | sudo tee /etc/sudoers.d/$_MEDIC_USER >/dev/null"
        echo "  sudo chmod 0440 /etc/sudoers.d/$_MEDIC_USER && sudo visudo -cf /etc/sudoers.d/$_MEDIC_USER"
        echo "Or leave VIGIL_MEDIC_ENABLED unset to keep Medic off."
    } >&2
}

# Returns 0 when Medic is off, running or started; 1 when it is on but couldn't
# start (the reason and the fix are printed). Never stops Vigil from starting.
medic_host_start() {
    medic_host_enabled || return 0
    _medic_warning

    local pidfile="$REPO_ROOT/logs/medic.pid" log="$REPO_ROOT/logs/medic.log"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "Medic: already running (pid $(cat "$pidfile"))."
        return 0
    fi

    local data runtime uid readable
    data=$(_medic_data_dir)
    runtime="${VIGIL_MEDIC_HOST_RUNTIME:-/opt/vigil-medic}"
    if [ "$(id -un)" = "$_MEDIC_USER" ]; then
        echo "Medic not started: Vigil itself is running as $_MEDIC_USER; Medic needs a user of its own." >&2
        return 1
    fi
    uid=$(id -u "$_MEDIC_USER" 2>/dev/null) || { _medic_setup "there is no OS user $_MEDIC_USER"; return 1; }
    if [ "$(_medic_stat "$data")" != "$uid 700" ]; then
        _medic_setup "$data must exist, be owned by $_MEDIC_USER and have mode 0700"
        return 1
    fi
    if [ ! -d "$runtime" ] || [ ! -w "$runtime" ]; then
        _medic_setup "$runtime must exist and be writable by $(id -un)"
        return 1
    fi

    # The loop goes first: it is the only command the sudo rule allows, so the
    # checks below run through it. A new inode, so a running copy isn't rewritten.
    if ! { mkdir -p "$runtime/bin" && chmod 0755 "$runtime/bin" \
        && cp "$REPO_ROOT/scripts/medic/host-native/medic-loop.sh" "$runtime/bin/.medic-loop.$$" \
        && chmod 0755 "$runtime/bin/.medic-loop.$$" \
        && mv -f "$runtime/bin/.medic-loop.$$" "$runtime/bin/medic-loop"; }; then
        echo "Medic not started: couldn't install $runtime/bin/medic-loop." >&2
        return 1
    fi

    # Privilege-model check 8: Medic's user can't read Vigil's secrets. Asked of
    # the real user through sudo, so it is the answer that matters, not a guess
    # from file modes.
    local state="${VIGIL_DIR:-$HOME/.vigil}"
    if ! readable=$(sudo -n -u "$_MEDIC_USER" -- "$runtime/bin/medic-loop" --probe \
        "$state/master.key" "$state/secrets.enc" "$state/jwt_secret" "$REPO_ROOT/.env" 2>&1); then
        _medic_setup "no sudo rule lets $(id -un) run $runtime/bin/medic-loop as $_MEDIC_USER ($readable)"
        return 1
    fi
    if [ -n "$readable" ]; then
        {
            echo "Medic not started: $_MEDIC_USER can read Vigil secrets (privilege-model check 8):"
            printf '%s\n' "$readable" | sed 's/^/  /'
            echo "Make each readable by its owner only, e.g.:"
            printf '%s\n' "$readable" | while IFS= read -r f; do
                printf '  chmod 0600 %q\n' "$f"
            done
            printf '  chmod 0700 %q\n' "$state"
        } >&2
        return 1
    fi

    # Medic's code, as the image ships it (no test material), then its own venv
    # from its own lock. The interpreter goes in the runtime dir too: uv's default
    # is under this user's home, which vigil-medic can't read.
    if ! (
        umask 022
        rm -rf "$runtime/app.new" "$runtime/app.old"
        mkdir -p "$runtime/app.new/services/medic" || exit 1
        cp -R "$REPO_ROOT/services/medic/." "$runtime/app.new/services/medic/" || exit 1
        rm -rf "$runtime/app.new/services/medic/"{.venv,tests} \
            "$runtime/app.new/services/medic/contracts/"{tests,tools,fixtures}
        find "$runtime/app.new" \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) \
            -prune -exec rm -rf {} +
        if [ -d "$runtime/app" ]; then mv "$runtime/app" "$runtime/app.old" || exit 1; fi
        mv "$runtime/app.new" "$runtime/app" && rm -rf "$runtime/app.old"
    ); then
        echo "Medic not started: couldn't copy its code into $runtime/app." >&2
        return 1
    fi
    ensure_uv || { echo "Medic not started: uv isn't available to build its venv." >&2; return 1; }
    if ! (
        umask 022
        export UV_PROJECT_ENVIRONMENT="$runtime/venv" UV_PYTHON_INSTALL_DIR="$runtime/python"
        "$UV" sync --project "$runtime/app/services/medic" --frozen --no-dev \
            --python-preference only-managed --link-mode copy --quiet \
            && "$runtime/venv/bin/python" -m compileall -q "$runtime/app/services/medic" >/dev/null
    ); then
        echo "Medic not started: building its venv from services/medic/uv.lock failed." >&2
        return 1
    fi

    rotate_log "$log"
    nohup sudo -n -u "$_MEDIC_USER" -- "$runtime/bin/medic-loop" \
        --python "$runtime/venv/bin/python" --app "$runtime/app" --data-dir "$data" \
        < /dev/null > "$log" 2>&1 &
    echo $! > "$pidfile"
    echo "Medic: started as $_MEDIC_USER (log: logs/medic.log). Health:"
    echo "  sudo -u $_MEDIC_USER $runtime/bin/medic-loop --python $runtime/venv/bin/python" \
        "--app $runtime/app --data-dir $(printf '%q' "$data") --check"
}
