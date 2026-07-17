#!/usr/bin/env bash
# First-run setup for the Vigil desktop app: venv + Python deps + frontend deps
# + a production SPA build (so the backend serves the UI at :6987, no Vite dev
# server). This is setup_dev.sh plus `npm run build`. Idempotent; safe to re-run.
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

# The backend serves frontend/build at :6987 (backend/main.py). The desktop
# window loads that, so we build the SPA rather than run the Vite dev server.
step frontend-build start
(cd "$REPO_ROOT/frontend" && npm run build) >&2 || { step frontend-build fail; exit 1; }
step frontend-build ok

step setup done
