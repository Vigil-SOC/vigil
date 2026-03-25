#!/bin/bash
# Start Vigil SOC in daemon mode (background) - Updated for v2.0

echo "=========================================="
echo "Vigil SOC v2.0 - Background Mode"
echo "=========================================="

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

# Create logs directory
mkdir -p logs

# Check if already running
if pgrep -f "uvicorn backend.main:app" > /dev/null; then
    echo "⚠️  Backend already running!"
    echo "   To stop: ./shutdown_all.sh"
    echo "   To view logs: tail -f logs/backend.log"
    exit 1
fi

# Initialize git submodules if needed
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

# Check venv - auto-create if missing
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating..."
    "$PYTHON" -m venv venv
fi

source venv/bin/activate

echo ""
echo "Checking Python dependencies..."
pip install -q --upgrade pip
_reqs=$(_filtered_reqs)
if pip install -q -r "$_reqs"; then
    echo "✓ Python dependencies installed"
else
    echo "⚠️  Some packages failed. Core functionality should work."
fi
rm -f "$_reqs"

# Verify uvicorn is available
if ! command -v uvicorn &> /dev/null; then
    echo "uvicorn not found. Installing dependencies..."
    pip install -q --upgrade pip
    _reqs=$(_filtered_reqs)
    pip install -q -r "$_reqs" || true
    rm -f "$_reqs"
    if ! command -v uvicorn &> /dev/null; then
        echo "❌ Critical: uvicorn not available. Run ./start_web.sh for full setup."
        exit 1
    fi
fi

# Load env vars
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
elif [ -f "env.example" ]; then
    echo "⚠️  .env not found. Creating from env.example..."
    cp env.example .env
    echo "✓ Created .env — edit to add your ANTHROPIC_API_KEY"
    set -a
    source .env
    set +a
fi

# Determine docker compose command (v2 plugin vs v1 standalone)
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

# Check and start PostgreSQL if needed
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
            if docker exec deeptempo-postgres pg_isready -U deeptempo -d deeptempo_soc &> /dev/null 2>&1; then
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

    # Initialize default admin user
    echo ""
    echo "Initializing default admin user..."
    python3 scripts/init_default_user.py || {
        echo "⚠️  Could not initialize default user."
        echo "   If PostgreSQL just started, it may need a moment. The user may already exist."
    }
else
    echo "⚠️  Docker not found. Database functionality will be limited."
fi

# Export Python path
export PYTHONPATH="${PWD}:${PYTHONPATH}"

# Start backend in background
echo "Starting backend server..."
nohup uvicorn backend.main:app \
    --host 127.0.0.1 \
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

# Start SOC daemon (SIEM poller)
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

# Install frontend dependencies if needed
if [ -d "frontend" ] && [ -f "frontend/package.json" ]; then
    if [ ! -d "frontend/node_modules" ]; then
        echo "Installing frontend dependencies..."
        cd frontend
        npm install --silent
        cd ..
        echo "✓ Frontend dependencies installed"
    fi
fi

# Start frontend if available
if [ -d "frontend/node_modules" ]; then
    echo "Starting frontend server..."
    cd frontend
    nohup npm run dev > ../logs/frontend-app.log 2>&1 &
    FRONTEND_PID=$!
    cd ..
    echo $FRONTEND_PID > logs/frontend.pid
    
    sleep 2
    if ps -p $FRONTEND_PID > /dev/null; then
        echo "✅ Frontend started (PID: $FRONTEND_PID)"
    else
        echo "⚠️  Frontend failed. Check logs/frontend-app.log"
    fi
fi

echo ""
echo "=========================================="
echo "✅ AI SOC Running in Background!"
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

