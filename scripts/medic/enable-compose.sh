#!/usr/bin/env bash
# scripts/medic/enable-compose.sh — get Medic ready to run on Docker Compose.
#
#   scripts/medic/enable-compose.sh [--secrets-only] [--rotate]
#
# 1. Writes Medic's two secrets as files (random, 0600, in a 0700 dir outside
#    the repo), unless they already exist. Never prints them.
#      viewer_password  the Viewer login the gateway uses (C3 §4.6, K1 T-34)
#      api_key          X-Medic-Key (X2): what the backend presents to Medic's API
# 2. Builds the three Medic images.
# 3. On Linux, hands each file to the uid that reads it (10002 gateway, 10001
#    Medic). Compose ignores uid/mode on file secrets and bind-mounts them as
#    they are, so a 0600 file the operator owns can't be read in the container.
#    Docker Desktop's file sharing needs no change.
# 4. Prints the one manual step left (create the Viewer account; V1 automates
#    it) and the exact `up` command, with this host's Docker socket gid.
#
#   --secrets-only  step 1 and 4 only: no Docker
#   --rotate        replace existing secrets. Running containers keep the old
#                   value (Compose bind-mounts the old file), so change the
#                   Viewer account's password, then recreate medic and
#                   medic-gateway (the script prints the command). Otherwise the
#                   gateway stops after two refusals (S5-4).
#
# Secrets dir: $VIGIL_MEDIC_SECRETS_DIR (an absolute path), default
# ~/.vigil-medic/secrets, deliberately not inside Vigil's ~/.vigil; the compose
# file reads the same variable with the same default.
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
viewer_user="${VIGIL_MEDIC_VIEWER_USER:-medic-viewer}"
base="$REPO_ROOT/infra/docker/docker-compose.yml"
overlay="$REPO_ROOT/infra/docker/medic/docker-compose.medic.yml"

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

# Sets $made to kept|created. Called directly, never inside $(...): bash turns
# `set -e` off in command substitutions, which would hide a failed write.
# Plain variables, not an associative array: macOS ships bash 3.2.
make_secret() {
    local name="$1" file="$dir/$1" tmp value
    if [ -s "$file" ] && [ "$rotate" = 0 ]; then
        # Unreadable here means it was handed to the container's uid (Linux).
        if [ -r "$file" ] && ! is_secret "$(cat "$file")"; then
            echo "error: $file isn't a secret this script wrote; re-run with --rotate" >&2
            exit 1
        fi
        made=kept
        return
    fi
    # 256 bits as 64 hex characters: alphanumeric, so it survives any login form.
    value="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
    is_secret "$value" || {
        echo "error: couldn't read 32 random bytes from /dev/urandom" >&2
        exit 1
    }
    # mktemp creates the file 0600 in the same dir, so the rename is atomic and
    # the secret is never readable by anyone else, even briefly.
    tmp="$(mktemp "$dir/.$name.XXXXXX")"
    printf '%s\n' "$value" > "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$file"
    made=created
}
made=
make_secret viewer_password
viewer_outcome="$made"
make_secret api_key
api_outcome="$made"

if [ -n "${VIGIL_MEDIC_DOCKER_GID:-}" ]; then
    docker_gid="$VIGIL_MEDIC_DOCKER_GID"
elif [ "$(uname -s)" = Linux ] && [ -S /var/run/docker.sock ]; then
    docker_gid="$(stat -c %g /var/run/docker.sock)"
else
    docker_gid=0 # Docker Desktop: the socket inside its VM is root:root 0660
fi

if [ "$secrets_only" = 0 ]; then
    dc -f "$overlay" --profile medic build medic medic-gateway medic-dockerproxy
    if [ "$(uname -s)" = Linux ]; then
        own="chown 10002:10002 /s/viewer_password && chown 10001:10001 /s/api_key"
        if [ "$(id -u)" = 0 ]; then
            chown 10002:10002 "$dir/viewer_password"
            chown 10001:10001 "$dir/api_key"
        else
            # Anyone who can run this can already drive Docker as root; this just
            # avoids asking for sudo. --network none: the container needs nothing.
            docker run --rm --network none --user 0:0 -v "$dir:/s" \
                --entrypoint sh vigil-medic-gateway:local -c "$own"
        fi
    fi
fi

echo "Medic secrets in $dir:"
echo "  viewer_password  $viewer_outcome"
echo "  api_key          $api_outcome"
if [ "$secrets_only" = 1 ] && [ "$(uname -s)" = Linux ]; then
    echo "  (--secrets-only on Linux: the files are still yours; run without it before 'up')"
fi
cat <<EOF

One manual step (until V1 automates it):
  As a Vigil admin, create the user "$viewer_user" with the Viewer role and the
  password in $dir/viewer_password
EOF
if [ "$(uname -s)" = Linux ] && [ "$secrets_only" = 0 ]; then
    echo "  (the file now belongs to uid 10002: read it with sudo)"
fi
cat <<EOF

Then start Medic (both -f files and the profile, on every compose command that
should keep it; add --profile daemon if you run the daemon):
  VIGIL_MEDIC_ENABLED=true VIGIL_MEDIC_DOCKER_GID=$docker_gid VIGIL_MEDIC_SECRETS_DIR=$dir \\
  docker compose -f $base \\
    -f $overlay --profile medic up -d
EOF
if [ "$rotate" = 1 ]; then
    cat <<EOF2

After --rotate: running containers still hold the old files. Once the Viewer
account has the new password, recreate them (same variables and files as above):
  docker compose -f $base -f $overlay --profile medic \\
    up -d --force-recreate medic medic-gateway
EOF2
fi
