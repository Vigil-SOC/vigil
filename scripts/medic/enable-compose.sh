#!/usr/bin/env bash
# scripts/medic/enable-compose.sh — turn Medic on for a Docker Compose install.
#
#   scripts/medic/enable-compose.sh [--secrets-only] [--rotate]
#
# One command, safe to re-run. It never prints a secret.
# 1. Secrets, as files (random, 0600, in a 0700 dir outside the repo), kept if
#    they already exist:
#      viewer_username  medic-<12 random a-z0-9>: the gateway's Viewer login
#                       (D2-17: not guessable)
#      viewer_password  that login's password (C3 §4.6, K1 T-34)
#      api_key          X-Medic-Key (X2): what the backend presents to Medic's API
# 2. compose.env beside them: VIGIL_MEDIC_ENABLED=true and the host-specific
#    values Compose needs. Every later compose command that should keep Medic
#    passes it (the script prints the command).
# 3. Builds the three Medic images.
# 4. Starts (or recreates) the backend with the flag on, then creates the
#    Viewer service account inside it: `python -m core.auth.service_account`,
#    the password on stdin, never on a command line. Lockout-exempt, Viewer only.
# 5. On Linux, hands each secret file to the uid that reads it (10002 gateway,
#    10001 Medic). Compose ignores uid/mode on file secrets and bind-mounts them
#    as they are; Docker Desktop's file sharing needs no change.
# 6. Starts Medic, its gateway and Docker proxy, and recreates the running Vigil
#    services the overlay puts on medic-net.
#
#   --secrets-only  steps 1 and 2 only: no Docker
#   --rotate        new viewer_password and api_key (the name stays). Changes the
#                   account's password and recreates medic and medic-gateway,
#                   which hold the old files until then.
#
# Environment:
#   VIGIL_MEDIC_SECRETS_DIR   absolute path, default ~/.vigil-medic/secrets (not
#                             inside Vigil's ~/.vigil); compose reads the same.
#   VIGIL_MEDIC_DOCKER_GID    the Docker socket's gid (default: found on Linux,
#                             0 on Docker Desktop)
#   VIGIL_MEDIC_COMPOSE_OVERRIDE  more compose files, colon-separated, last on
#                             every command (an operator's own override; the
#                             live tests' stubs)
# Vigil's own settings come from the repo .env, as for `docker compose
# --env-file .env` (README; VIGIL_MEDIC_DOTENV names another file); variables
# already in the environment win over it, and Medic's compose.env over both.
set -euo pipefail

usage() {
    echo "usage: $0 [--secrets-only] [--rotate]" >&2
    exit 2
}

secrets_only=0
rotate=0
for arg in "$@"; do
    case "$arg" in
        --secrets-only) secrets_only=1 ;;
        --rotate) rotate=1 ;;
        *) usage ;;
    esac
done

# shellcheck source=SCRIPTDIR/../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"

dir="${VIGIL_MEDIC_SECRETS_DIR:-${HOME:-}/.vigil-medic/secrets}"
case "$dir" in
    /*) ;;
    *)
        # Compose would resolve a relative path against infra/docker/, this
        # script against the current directory: two different places.
        echo "error: VIGIL_MEDIC_SECRETS_DIR must be an absolute path" >&2
        exit 1
        ;;
esac
base="$REPO_ROOT/infra/docker/docker-compose.yml"
overlay="$REPO_ROOT/infra/docker/medic/docker-compose.medic.yml"
settings="$dir/compose.env"

mode_of() {
    if [ "$(uname -s)" = Darwin ]; then stat -f %Lp "$1"; else stat -c %a "$1"; fi
}

if [ -e "$dir" ]; then
    if [ "$(mode_of "$dir")" != 700 ]; then
        echo "error: $dir must be mode 0700 (it is $(mode_of "$dir")); fix it or pick another VIGIL_MEDIC_SECRETS_DIR" >&2
        exit 1
    fi
else
    mkdir -p "$(dirname "$dir")"
    mkdir -m 0700 "$dir"
fi
if [ ! -w "$dir" ] || [ ! -x "$dir" ]; then
    echo "error: $dir isn't writable by $(id -un) (created by another user, e.g. with sudo?)" >&2
    exit 1
fi

is_secret() {
    printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'
}
# X2's X-Medic-Key: 32 random bytes, base64url, no padding (the gateway's check).
is_api_key() {
    printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_-]{43}$'
}
is_username() {
    printf '%s' "$1" | grep -Eq '^medic-[a-z0-9]{12}$'
}

# Writes $2 to $dir/$1, 0600 from creation: mktemp in the same dir, then an
# atomic rename, so the value is never readable by anyone else, even briefly.
write_private() {
    local tmp
    tmp="$(mktemp "$dir/.$1.XXXXXX")"
    printf '%s\n' "$2" > "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$dir/$1"
}

# Sets $made to kept|created. Called directly, never inside $(...): bash turns
# `set -e` off in command substitutions, which would hide a failed write.
# Plain variables, not an associative array: macOS ships bash 3.2.
make_secret() {
    local name="$1" file="$dir/$1" value check=is_secret
    [ "$name" = api_key ] && check=is_api_key
    if [ -s "$file" ] && [ "$rotate" = 0 ]; then
        # Unreadable here means it was handed to the container's uid (Linux).
        if [ -r "$file" ] && ! "$check" "$(cat "$file")"; then
            echo "error: $file isn't a secret this script wrote; re-run with --rotate" >&2
            exit 1
        fi
        made=kept
        return
    fi
    if [ "$name" = api_key ]; then
        value="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
    else
        # 256 bits as 64 hex characters: alphanumeric, so it survives any login form.
        value="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
    fi
    "$check" "$value" || {
        echo "error: couldn't read 32 random bytes from /dev/urandom" >&2
        exit 1
    }
    write_private "$name" "$value"
    made=created
}

# Not a credential, but not guessable either (D2-17): anyone who knows a
# login's name can try passwords against it. Kept on --rotate.
make_username() {
    local file="$dir/viewer_username" value
    if [ -s "$file" ]; then
        if ! is_username "$(cat "$file")"; then
            echo "error: $file doesn't hold a medic-<12 a-z0-9> name; remove it to mint a new one" >&2
            exit 1
        fi
        made=kept
        return
    fi
    # 12 of a-z0-9 from /dev/urandom (~62 bits). A finite read, not
    # `tr < /dev/urandom | head`: under pipefail that SIGPIPEs tr. 512 bytes keep
    # ~72 characters; is_username catches the (negligible) short draw.
    # LC_ALL=C: tr on bytes.
    value="$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9')"
    value="medic-${value:0:12}"
    is_username "$value" || {
        echo "error: couldn't mint a random username" >&2
        exit 1
    }
    write_private viewer_username "$value"
    made=created
}

made=
make_username
user_outcome="$made"
make_secret viewer_password
viewer_outcome="$made"
make_secret api_key
api_outcome="$made"
viewer_user="$(cat "$dir/viewer_username")"

if [ -n "${VIGIL_MEDIC_DOCKER_GID:-}" ]; then
    docker_gid="$VIGIL_MEDIC_DOCKER_GID"
elif [ "$(uname -s)" = Linux ] && [ -S /var/run/docker.sock ]; then
    docker_gid="$(stat -c %g /var/run/docker.sock)"
else
    docker_gid=0 # Docker Desktop: the socket inside its VM is root:root 0660
fi

# Rewritten every run, so it always matches this host. No secret in it.
write_private compose.env "# Medic on Compose: written by scripts/medic/enable-compose.sh. Pass it
# (after Vigil's own .env) on every compose command that should keep Medic.
VIGIL_MEDIC_ENABLED=true
VIGIL_MEDIC_SECRETS_DIR=$dir
VIGIL_MEDIC_DOCKER_GID=$docker_gid
VIGIL_MEDIC_VIEWER_USER=$viewer_user"

# Recreating the backend re-renders its whole environment, and shell env beats
# every --env-file, so the shell must hold the right values, in this order:
#   1. Vigil's .env (otherwise lib.sh's dc() hands the backend its default
#      OLLAMA_URL over the one in .env); no copy of env.example if there's none;
#   2. what the caller exported, which wins over .env (start.sh's rule, and the
#      README's exported JWT_SECRET_KEY);
#   3. compose.env last: env.example's VIGIL_MEDIC_ENABLED="false" must not
#      switch Medic off.
dotenv="${VIGIL_MEDIC_DOTENV:-$REPO_ROOT/.env}"
if [ -f "$dotenv" ]; then
    caller="$(export -p)"
    set -a
    # shellcheck source=/dev/null
    source "$dotenv"
    set +a
    eval "$caller"
fi
set -a
# shellcheck source=/dev/null
source "$settings"
set +a
env_files=()
[ -f "$dotenv" ] && env_files+=(--env-file "$dotenv")
env_files+=(--env-file "$settings")

# Every compose call with Medic on: Vigil's .env, Medic's settings, the overlay.
# lib.sh's dc adds the overlay and VIGIL_MEDIC_COMPOSE_OVERRIDE's files itself
# now that compose.env exists (V1-4), as it does for start.sh.
dcm() {
    dc "${env_files[@]}" --profile medic "$@"
}

# Prints a secret on stdout, for a pipe only. On Linux a re-run finds the file
# already handed to the container's uid, so read it as root in a throwaway
# container with no network.
read_secret() {
    if [ -r "$dir/$1" ]; then
        cat "$dir/$1"
    else
        docker run --rm --network none --user 0:0 -v "$dir:/s:ro" \
            --entrypoint cat vigil-medic-gateway:local "/s/$1"
    fi
}

# Whether the backend's rendered config has a line matching $1. The secrets in
# it go down a pipe, never to the screen. No grep -q: exiting early would
# SIGPIPE compose, and pipefail would turn a match into a failure.
backend_renders() {
    dcm config backend | grep -E "$1" > /dev/null
}

ensure_account() {
    local err rc running up=0 _
    for _ in $(seq 1 60); do
        # An assignment, so a failing compose call stops the run (set -e)
        # rather than reading as a backend that isn't up.
        running="$(dcm ps --status running --services)"
        if printf '%s\n' "$running" | grep -Fx backend > /dev/null; then
            up=1
            break
        fi
        sleep 3
    done
    if [ "$up" = 0 ]; then
        echo "error: the backend container isn't running; see: docker compose logs backend" >&2
        exit 1
    fi
    err="$(mktemp)"
    # Only 75 (schema or Viewer role not there yet: db-seed still running) is
    # retried. Anything else is final: 64 usage, 70 database error, 77 refused,
    # 1 an image without this code (rebuild the backend).
    for _ in $(seq 1 60); do
        rc=0
        read_secret viewer_password \
            | dcm exec -T backend python -m core.auth.service_account ensure "$viewer_user" \
            2> "$err" || rc=$?
        case "$rc" in
            0) rm -f "$err"; return 0 ;;
            75) ;;
            *) break ;;
        esac
        sleep 3
    done
    echo "error: couldn't create the Medic service account (exit $rc):" >&2
    cat "$err" >&2
    rm -f "$err"
    exit 1
}

if [ "$secrets_only" = 1 ] && { [ "$viewer_outcome" = created ] || [ "$api_outcome" = created ]; }; then
    # The next full run must recreate medic and the gateway even though it
    # finds these files "kept".
    : > "$dir/.recreate"
fi
if [ "$secrets_only" = 0 ]; then
    # Recreating the backend re-renders its environment. Refuse if this shell
    # would hand it no JWT secret, which it won't start without (README).
    # Fails the run (set -e) if compose can't render at all.
    dcm config backend > /dev/null
    if backend_renders '^ *JWT_SECRET_KEY: ("")?$' \
        && ! backend_renders '^ *DEV_MODE: "?true"?$'; then
        echo "error: the backend would get an empty JWT_SECRET_KEY. Run this from the shell, or with the $REPO_ROOT/.env, you start Vigil with." >&2
        exit 1
    fi
    dcm build medic medic-gateway medic-dockerproxy
    # The backend polls Medic with api_key (V2). Compose bind-mounts the file, so
    # a new one reaches a running backend only on a recreate.
    if [ "$api_outcome" = created ] || [ -e "$dir/.recreate" ]; then
        dcm up -d --force-recreate backend
    fi
    # db-seed seeds the Viewer role; only a full `up` would start it otherwise.
    dcm up -d backend db-seed
    ensure_account
    if [ "$(uname -s)" = Linux ]; then
        # api_key: Medic (uid 10001) owns it; the backend (gid 1000) polls with it.
        own="chown 10002:10002 /s/viewer_password && chown 10001:1000 /s/api_key && chmod 0640 /s/api_key"
        if [ "$(id -u)" = 0 ]; then
            chown 10002:10002 "$dir/viewer_password"
            chown 10001:1000 "$dir/api_key"
            chmod 0640 "$dir/api_key"
        else
            # Anyone who can run this can already drive Docker as root; this just
            # avoids asking for sudo. --network none: the container needs nothing.
            docker run --rm --network none --user 0:0 -v "$dir:/s" \
                --entrypoint sh vigil-medic-gateway:local -c "$own"
        fi
    fi
    if [ "$viewer_outcome" = created ] || [ "$api_outcome" = created ] \
        || [ -e "$dir/.recreate" ]; then
        # A new file (--rotate, or one deleted and re-minted): Compose
        # bind-mounts the old inode, so only a recreate sees it. Straight after
        # the password changed, since the gateway stops after two refusals.
        dcm up -d --force-recreate medic medic-gateway
        rm -f "$dir/.recreate"
    fi
    # Only the medic-net members already running: enabling Medic starts nothing
    # else of Vigil's. Docker drops them off medic-net unless recreated with
    # the overlay. An assignment, so a failed `ps` stops the run (set -e).
    running="$(dcm ps --status running --services)"
    members=()
    for svc in $running; do
        case "$svc" in soc-daemon | agent-worker | agent-serve) members+=("$svc") ;; esac
    done
    dcm up -d medic medic-gateway medic-dockerproxy ${members[@]+"${members[@]}"}
fi

echo "Medic secrets in $dir:"
echo "  viewer_username  $user_outcome"
echo "  viewer_password  $viewer_outcome"
echo "  api_key          $api_outcome"
if [ "$secrets_only" = 1 ]; then
    cat <<EOF

--secrets-only: nothing started. Run without it to build the images, create the
Viewer service account and start Medic.
EOF
    if [ "$rotate" = 1 ]; then
        cat <<EOF2
After --rotate the running containers still hold the old files: the full run
changes the account's password, recreates the backend and does
  up -d --force-recreate medic medic-gateway
EOF2
    fi
else
    echo
    echo "Medic is on: service account ready, medic, medic-gateway and medic-dockerproxy started."
fi
env_hint=""
[ -f "$REPO_ROOT/.env" ] && env_hint="--env-file $REPO_ROOT/.env "
cat <<EOF

./start.sh and Vigil's service controls keep Medic's overlay from now on. A
docker compose command you type yourself needs Medic's settings and overlay
(add --profile daemon if you run the daemon):
  docker compose ${env_hint}--env-file $settings \\
    -f $base \\
    -f $overlay --profile medic up -d
EOF
