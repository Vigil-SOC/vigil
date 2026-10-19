#!/bin/bash
# Issue a password reset link on a native install, for when no mail backend is
# set up. Usage: scripts/reset-password.sh --list | <username|email> [--base-url URL]
source "$(dirname "$0")/lib.sh"

load_env
# The link must be signed with the running backend's secret. ensure_jwt_secret
# would mint a new one when it's missing, and the server would reject the link.
if [ -z "${JWT_SECRET_KEY:-}" ]; then
    secret_file="${VIGIL_DIR:-$HOME/.vigil}/jwt_secret"
    [ -s "$secret_file" ] || {
        echo "No JWT secret: set JWT_SECRET_KEY or start Vigil once with ./start.sh ($secret_file is missing)." >&2
        exit 1
    }
    JWT_SECRET_KEY="$(cat "$secret_file")"
    export JWT_SECRET_KEY
fi

[ -x "$REPO_ROOT/venv/bin/python" ] || { echo "No venv; run ./start.sh first." >&2; exit 1; }
source "$REPO_ROOT/venv/bin/activate"
exec python3 "$REPO_ROOT/scripts/reset_password.py" "$@"
