#!/usr/bin/env bash
# First-run setup for the Vigil desktop app: venv + Python deps + frontend deps.
# The SPA build is deliberately NOT done here — app_up.sh is the sole builder
# (it builds in real-auth mode and owns the .vigil-real-auth marker). Building
# here too would produce a dev-auth bundle that app_up.sh discards and rebuilds
# on the very next step, doubling a slow React/MUI build on every first launch.
# Essentially setup_dev.sh. Idempotent; safe to re-run.
#
# Emits machine-parseable `STEP <phase> <status>` lines on stdout for the
# Electron splash to parse (status: start|ok|fail). Human detail goes to stderr.
source "$(dirname "$0")/lib.sh"

step() { echo "STEP $1 $2"; }

step python start
ensure_uv >&2 || { step python fail; exit 1; }
step python ok

step env start
load_env
step env ok

step venv start
ensure_venv >&2 || { step venv fail; exit 1; }
step venv ok

step deps start
install_python_deps >&2 || { step deps fail; exit 1; }
step deps ok

step frontend-deps start
if ensure_npm_on_path && [ -d "$REPO_ROOT/clients/web" ]; then
    if [ ! -d "$REPO_ROOT/clients/web/node_modules" ]; then
        (cd "$REPO_ROOT/clients/web" && npm ci --prefer-offline) >&2 \
            || (cd "$REPO_ROOT/clients/web" && npm install) >&2
    fi
    step frontend-deps ok
else
    echo "npm/frontend not found; the app window needs the built SPA." >&2
    step frontend-deps fail
    exit 1
fi

# No SPA build here — app_up.sh builds it (in real-auth mode) right after this
# script returns. See the header for why building here would be wasted work.
step setup done
