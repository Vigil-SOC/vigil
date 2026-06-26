#!/bin/bash
# Start Vigil SOC v2.0
#
# Usage:
#   ./start.sh              # foreground — occupies terminal, Ctrl+C to stop
#   ./start.sh -d           # background daemon — returns to shell, logs to logs/
#   ./start.sh --daemon     # same as -d

set -e

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
DAEMON_MODE=0
for arg in "$@"; do
    case "$arg" in
        -d|--daemon) DAEMON_MODE=1 ;;
        *)
            echo "❌ Unknown argument: $arg"
            echo "   Usage: $0 [--daemon|-d]"
            exit 1
            ;;
    esac
done

if [ "$DAEMON_MODE" -eq 1 ]; then
    echo "=========================================="
    echo "Vigil SOC v2.0 - Background Mode"
    echo "=========================================="
else
    echo "=========================================="
    echo "Vigil SOC v2.0 - Startup"
    echo "=========================================="
fi

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

# Require Python 3.10+ (claude-agent-sdk and other deps need it)
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &> /dev/null; then
        ver=$("$candidate" -c 'import sys; print(sys.version_info >= (3,10))' 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.10+ is required but not found."
    echo "   Install it from https://python.org or via your package manager."
    exit 1
fi

# Check Docker (required for PostgreSQL and Redis)
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed (required for PostgreSQL and Redis)."
    echo "   Install from: https://docs.docker.com/engine/install/"
    exit 1
fi

# Check Node.js (required for frontend). Set SKIP_FRONTEND=1 when the node
# runtime is missing or too old so the frontend startup block below actually
# honours the warning instead of silently attempting to run npm anyway.
SKIP_FRONTEND=0
if ! command -v node &> /dev/null; then
    echo "⚠️  Node.js not found. Frontend will not start."
    echo "   Install from: https://nodejs.org/"
    SKIP_FRONTEND=1
elif ! node -e "process.exit(parseInt(process.version.slice(1)) >= 18 ? 0 : 1)" 2>/dev/null; then
    echo "⚠️  Node.js 18+ is required for frontend. Found: $(node --version). Frontend will not start."
    SKIP_FRONTEND=1
fi

# In daemon mode, ensure logs directory exists and check for existing process
if [ "$DAEMON_MODE" -eq 1 ]; then
    mkdir -p logs
    if pgrep -f "uvicorn backend.main:app" > /dev/null; then
        echo "⚠️  Backend already running!"
        echo "   To stop: ./shutdown_all.sh"
        echo "   To view logs: tail -f logs/backend.log"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Git submodules
# ---------------------------------------------------------------------------
if [ -d ".git" ]; then
    if [ ! -f "deeptempo-core/setup.py" ] && [ ! -f "deeptempo-core/pyproject.toml" ]; then
        echo "Initializing git submodules..."
        if git submodule update --init --recursive; then
            echo "✓ Git submodules initialized"
        else
            echo "⚠️  Failed to initialize submodules. Some features may not work."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------

# Build a filtered requirements file, skipping submodule editable installs
# whose directories aren't yet initialized (missing setup.py / pyproject.toml)
_filtered_reqs() {
    local tmp
    tmp=$(mktemp)
    while IFS= read -r line; do
        if [[ "$line" =~ ^-e[[:space:]]+\. ]]; then
            local dir="${line#*-e }"
            dir="${dir#*-e	}"   # handle tab separator
            if [ -f "$dir/setup.py" ] || [ -f "$dir/pyproject.toml" ]; then
                echo "$line"
            fi
            # else: submodule not initialized — skip silently
        else
            echo "$line"
        fi
    done < requirements.txt > "$tmp"
    echo "$tmp"
}

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating..."
    "$PYTHON" -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install/update dependencies
echo ""
echo "Checking Python dependencies..."
pip install -q --upgrade pip
_reqs=$(_filtered_reqs)
if pip install -q -r "$_reqs"; then
    echo "✓ Python dependencies installed"
else
    echo "⚠️  Some packages failed to install. Core functionality should work."
fi
rm -f "$_reqs"

# Verify uvicorn is available
if ! command -v uvicorn &> /dev/null; then
    echo "❌ uvicorn not found after pip install. Retrying..."
    _reqs=$(_filtered_reqs)
    pip install -q -r "$_reqs" || true
    rm -f "$_reqs"
    if ! command -v uvicorn &> /dev/null; then
        echo "❌ Critical: uvicorn still not available. Check requirements.txt"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

# Save caller-supplied BIND_HOST so it survives sourcing .env
_CALLER_BIND_HOST="${BIND_HOST}"
if [ -f ".env" ]; then
    echo "✓ Loading environment variables from .env"
    set -a
    source .env
    set +a
else
    echo "⚠️  Warning: .env file not found"
    echo "   Creating .env from env.example with defaults..."
    if [ -f "env.example" ]; then
        cp env.example .env
        echo "✓ Created .env from env.example"
        echo "   Configure LLM provider keys in the web UI:"
        echo "   Settings → AI / LLM Providers → Add provider"
        set -a
        source .env
        set +a
    else
        echo "❌ env.example not found either. Some features may not work."
    fi
fi
# Restore caller-supplied BIND_HOST if one was given, so
# `BIND_HOST=0.0.0.0 ./start.sh` actually wins over the value in .env.
if [ -n "$_CALLER_BIND_HOST" ]; then
    BIND_HOST="$_CALLER_BIND_HOST"
fi
unset _CALLER_BIND_HOST

# ---------------------------------------------------------------------------
# Docker services: PostgreSQL, Redis, Bifrost
# ---------------------------------------------------------------------------

# Determine docker compose command (v2 plugin vs v1 standalone)
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

echo ""
echo "Checking PostgreSQL database..."
if command -v docker &> /dev/null; then
    if docker ps --format '{{.Names}}' | grep -q "deeptempo-postgres"; then
        echo "✓ PostgreSQL is already running"
    else
        echo "Starting PostgreSQL..."
        cd docker
        $DOCKER_COMPOSE up -d postgres
        cd ..

        echo "Waiting for PostgreSQL..."
        for i in {1..30}; do
            if docker exec deeptempo-postgres pg_isready -U postgres &> /dev/null 2>&1; then
                echo "✓ PostgreSQL is ready!"
                break
            fi
            if [ $i -eq 30 ]; then
                echo "⚠️  PostgreSQL may not be ready"
            fi
            sleep 1
        done
    fi

    # Start Redis (LLM job queue)
    if docker ps --format '{{.Names}}' | grep -q "deeptempo-redis"; then
        echo "✓ Redis is already running"
    else
        echo "Starting Redis (LLM job queue)..."
        cd docker
        $DOCKER_COMPOSE up -d redis
        cd ..
        echo "Waiting for Redis..."
        sleep 2
        echo "✓ Redis started"
    fi

    # Start Bifrost (LLM gateway — single path for all model traffic).
    # Issue #292: without this, the backend's Anthropic SDK is pointed at
    # http://bifrost:8080/anthropic and any chat call dies on a connection
    # error to a host that isn't running. Bring it up alongside Postgres
    # and Redis so a fresh dev install just works.
    if docker ps --format '{{.Names}}' | grep -q "deeptempo-bifrost"; then
        echo "✓ Bifrost is already running"
    else
        echo "Starting Bifrost (LLM gateway)..."
        cd docker
        $DOCKER_COMPOSE up -d bifrost
        cd ..
        echo "✓ Bifrost started"
    fi
    # The container publishes :8080 to the host; native uvicorn can't
    # resolve the docker-internal hostname `bifrost`. If the user hasn't
    # explicitly set BIFROST_URL, point at the host-published port.
    if [ -z "${BIFROST_URL+x}" ] || [ "${BIFROST_URL}" = "http://bifrost:8080" ]; then
        export BIFROST_URL="http://localhost:8080"
        echo "✓ BIFROST_URL set to ${BIFROST_URL} for native run"
    fi

    # Initialize database schema (SQLAlchemy Base.metadata.create_all)
    # Runs BEFORE init_default_user.py because user/role tables and all
    # case_* tables must exist before any bootstrap or API query.
    echo ""
    echo "Initializing database schema..."
    if ! python3 scripts/init_schema.py; then
        echo "❌ Database schema initialization failed."
        echo "   The backend will not start correctly without a valid schema."
        echo "   Check PostgreSQL connectivity and the error above."
        exit 1
    fi
    echo "✓ Database schema ready"

    # Initialize default admin user
    echo ""
    echo "Initializing default admin user..."
    python3 scripts/init_default_user.py || {
        echo "⚠️  Could not initialize default user."
        echo "   If PostgreSQL just started, it may need a moment. The user may already exist."
    }
else
    echo "⚠️  Docker not found. Database functionality limited."
fi

# ---------------------------------------------------------------------------
# Frontend dependencies
# ---------------------------------------------------------------------------
echo ""
echo "Checking frontend dependencies..."
if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend" ] && [ -f "frontend/package.json" ]; then
    # Reinstall when node_modules is absent OR stale. npm writes
    # frontend/node_modules/.package-lock.json after a successful install;
    # if package.json or package-lock.json is newer than that marker, the
    # installed tree predates a dependency change — e.g. pulling a commit
    # that added tailwindcss/autoprefixer. A missing-only check would say
    # "deps OK" and Vite then crashes at startup with
    # "Loading PostCSS Plugin failed: Cannot find module 'tailwindcss'".
    _fe_marker="frontend/node_modules/.package-lock.json"
    _fe_need_install=0
    if [ ! -d "frontend/node_modules" ] || [ ! -f "$_fe_marker" ]; then
        _fe_need_install=1
    elif [ "frontend/package.json" -nt "$_fe_marker" ]; then
        _fe_need_install=1
    elif [ -f "frontend/package-lock.json" ] && [ "frontend/package-lock.json" -nt "$_fe_marker" ]; then
        _fe_need_install=1
    fi

    if [ "$_fe_need_install" -eq 1 ]; then
        echo "Installing frontend dependencies (may take a few minutes)..."
        npm --prefix frontend install
        echo "✓ Frontend dependencies installed"
    else
        echo "✓ Frontend dependencies OK"
    fi
    unset _fe_marker _fe_need_install
fi

# ---------------------------------------------------------------------------
# Launch — foreground or daemon
# ---------------------------------------------------------------------------
export PYTHONPATH="${PWD}:${PYTHONPATH}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"

# Poll the backend health endpoint until it responds, so the frontend
# isn't started while uvicorn is still importing the app (the browser's
# initial API burst would otherwise spam vite proxy ECONNREFUSED errors).
wait_for_backend() {
    local host="$BIND_HOST"
    [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
    local url="http://${host}:6987/api/health"
    echo "Waiting for backend to be ready..."
    for i in {1..60}; do
        if curl -sf --max-time 2 "$url" > /dev/null 2>&1; then
            echo "✓ Backend is ready"
            return 0
        fi
        sleep 1
    done
    echo "⚠️  Backend not ready after 60s — starting frontend anyway"
    return 1
}

if [ "$DAEMON_MODE" -eq 0 ]; then
    # ------------------------------------------------------------------
    # FOREGROUND MODE
    # ------------------------------------------------------------------

    cleanup() {
        echo ""
        echo "Shutting down servers..."

        if [ -n "$WORKER_PID" ]; then
            echo "Stopping LLM worker (PID: $WORKER_PID)..."
            kill $WORKER_PID 2>/dev/null
            wait $WORKER_PID 2>/dev/null
        fi

        if [ -n "$BACKEND_PID" ]; then
            echo "Stopping backend (PID: $BACKEND_PID)..."
            kill $BACKEND_PID 2>/dev/null
            wait $BACKEND_PID 2>/dev/null
        fi

        if [ -n "$FRONTEND_PID" ]; then
            echo "Stopping frontend (PID: $FRONTEND_PID)..."
            kill $FRONTEND_PID 2>/dev/null
            wait $FRONTEND_PID 2>/dev/null
        fi

        pkill -f "uvicorn backend.main:app" 2>/dev/null
        pkill -f "arq services.llm_worker" 2>/dev/null
        pkill -f "vite" 2>/dev/null

        echo "✓ Servers stopped"
        echo ""
        echo "Database and Redis are still running. To stop:"
        echo "  cd docker && $DOCKER_COMPOSE stop postgres redis bifrost"
        exit 0
    }

    trap cleanup INT TERM EXIT

    echo ""
    echo "Starting backend API server..."
    uvicorn backend.main:app \
        --host "$BIND_HOST" \
        --port 6987 \
        --reload \
        --reload-dir backend \
        --reload-dir services \
        --reload-dir database &
    BACKEND_PID=$!
    sleep 2

    # Start ARQ LLM worker (processes queued LLM requests)
    echo "Starting LLM worker (ARQ)..."
    python3 -m services.run_llm_worker &
    WORKER_PID=$!
    sleep 1

    # Start frontend (skip when Node.js prereq unmet)
    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend/node_modules" ]; then
        wait_for_backend || true
        echo "Starting frontend dev server..."
        npm --prefix frontend run dev &
        FRONTEND_PID=$!
    elif [ "$SKIP_FRONTEND" -ne 0 ]; then
        echo "ℹ️  Skipping frontend startup (Node.js prerequisite unmet)."
        FRONTEND_PID=""
    else
        echo "⚠️  Frontend dependencies not installed"
        FRONTEND_PID=""
    fi

    echo ""
    echo "=========================================="
    echo "✅ Vigil SOC v2.0 - Ready!"
    echo "=========================================="
    echo "Backend API:   http://localhost:6987"
    echo "Frontend UI:   http://localhost:6988"
    echo "API Docs:      http://localhost:6987/docs"
    echo ""
    echo "🔐 Default Login Credentials:"
    echo "   Username: admin"
    echo "   Password: admin123"
    echo "   (⚠️  Change in production!)"
    if [ "$DEV_MODE" == "true" ]; then
        echo ""
        echo "⚠️  DEV_MODE ENABLED - Auth bypassed!"
    fi
    echo ""
    echo "Press Ctrl+C to stop"
    echo "=========================================="

    wait

else
    # ------------------------------------------------------------------
    # DAEMON (BACKGROUND) MODE
    # ------------------------------------------------------------------

    echo ""
    echo "Starting backend server..."
    nohup uvicorn backend.main:app \
        --host "$BIND_HOST" \
        --port 6987 \
        --reload \
        --reload-dir backend \
        --reload-dir services \
        --reload-dir database \
        > logs/backend.log 2>&1 &
    BACKEND_PID=$!
    echo $BACKEND_PID > logs/backend.pid
    sleep 3

    if ps -p $BACKEND_PID > /dev/null; then
        echo "✅ Backend started (PID: $BACKEND_PID)"
    else
        echo "❌ Backend failed. Check logs/backend.log"
        exit 1
    fi

    # Start SOC daemon (SIEM poller).
    # NOTE: The LLM worker (ARQ job processor for Claude API calls) is managed
    # dynamically by the SOC daemon via daemon/llm_worker_manager.py. It starts
    # and stops automatically when the orchestrator is enabled/disabled in the
    # Settings UI — no manual startup required here.
    echo "Starting SOC daemon (SIEM poller)..."
    nohup "${PWD}/venv/bin/python" daemon/main.py > logs/daemon.log 2>&1 &
    DAEMON_PID=$!
    echo $DAEMON_PID > logs/daemon.pid
    sleep 2

    if ps -p $DAEMON_PID > /dev/null; then
        echo "✅ SOC Daemon started (PID: $DAEMON_PID)"
    else
        echo "⚠️  SOC Daemon failed. Check logs/daemon.log"
    fi

    # Start frontend if available (skip when Node.js prereq unmet)
    if [ "$SKIP_FRONTEND" -eq 0 ] && [ -d "frontend/node_modules" ]; then
        wait_for_backend || true
        echo "Starting frontend server..."
        nohup npm --prefix frontend run dev > logs/frontend-app.log 2>&1 &
        FRONTEND_PID=$!
        echo $FRONTEND_PID > logs/frontend.pid
        sleep 2
        if ps -p $FRONTEND_PID > /dev/null; then
            echo "✅ Frontend started (PID: $FRONTEND_PID)"
        else
            echo "⚠️  Frontend failed. Check logs/frontend-app.log"
        fi
    elif [ "$SKIP_FRONTEND" -ne 0 ]; then
        echo "ℹ️  Skipping frontend startup (Node.js prerequisite unmet)."
    fi

    echo ""
    echo "=========================================="
    echo "✅ Vigil SOC v2.0 - Running in Background!"
    echo "=========================================="
    echo "Backend API:   http://localhost:6987"
    echo "Frontend UI:   http://localhost:6988"
    echo "API Docs:      http://localhost:6987/docs"
    echo ""
    echo "🔐 Default Login Credentials:"
    echo "   Username: admin"
    echo "   Password: admin123"
    echo "   (⚠️  Change in production!)"
    echo ""
    echo "📝 View logs:"
    echo "   Backend:  tail -f logs/backend.log"
    echo "   Daemon:   tail -f logs/daemon.log"
    echo "   Frontend: tail -f logs/frontend-app.log"
    echo ""
    echo "🛑 Stop servers:"
    echo "   ./shutdown_all.sh"
    echo ""
    echo "🔄 Hot-reload enabled!"
    if [ "$DEV_MODE" == "true" ]; then
        echo "⚠️  DEV_MODE ENABLED - Auth bypassed!"
    fi
    echo "=========================================="

fi
