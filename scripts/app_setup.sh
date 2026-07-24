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

step submodules start
if [ -d "$REPO_ROOT/.git" ] && [ ! -f "$REPO_ROOT/deeptempo-core/pyproject.toml" ] \
    && [ ! -f "$REPO_ROOT/deeptempo-core/setup.py" ]; then
    (cd "$REPO_ROOT" && git submodule update --init --recursive) >&2 \
        || echo "Warning: submodule init failed." >&2
fi
step submodules ok

step python start
PYTHON=$(find_python) || { step python fail; exit 1; }
step python ok

step env start
load_env
step env ok

step venv start
ensure_venv "$PYTHON" >&2 || { step venv fail; exit 1; }
step venv ok

step deps start
install_python_deps >&2
step deps ok

step frontend-deps start
if ensure_npm_on_path && [ -d "$REPO_ROOT/frontend" ]; then
    if [ ! -d "$REPO_ROOT/frontend/node_modules" ]; then
        (cd "$REPO_ROOT/frontend" && npm ci --prefer-offline) >&2 \
            || (cd "$REPO_ROOT/frontend" && npm install) >&2
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
