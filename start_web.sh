#!/bin/bash
# Start Vigil SOC Web Application (Updated for 2.0)

set -e

echo "=========================================="
echo "Vigil SOC v2.0 - Startup"
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

# Load environment variables
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
        echo "   Edit .env to add your ANTHROPIC_API_KEY and customize settings"
        set -a
        source .env
        set +a
    else
        echo "❌ env.example not found either. Some features may not work."
    fi
fi

# Check and start PostgreSQL database
echo ""
echo "Checking PostgreSQL database..."
if command -v docker &> /dev/null; then
    if docker ps --format '{{.Names}}' | grep -q "deeptempo-postgres"; then
        echo "✓ PostgreSQL is already running"
    else
        echo "Starting PostgreSQL..."
        cd docker
        docker compose up -d postgres
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
        docker compose up -d redis
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
    echo "⚠️  Docker not found. Database functionality limited."
fi

# Check and install frontend dependencies
echo ""
echo "Checking frontend dependencies..."
if [ -d "frontend" ]; then
    if [ ! -d "frontend/node_modules" ]; then
        echo "Installing frontend dependencies (may take a few minutes)..."
        cd frontend
        npm install
        cd ..
        echo "✓ Frontend dependencies installed"
    else
        echo "✓ Frontend dependencies OK"
    fi
fi

# Function to cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down servers..."
    
    if [ ! -z "$WORKER_PID" ]; then
        echo "Stopping LLM workers (launcher PID: $WORKER_PID)..."
        kill $WORKER_PID 2>/dev/null
        wait $WORKER_PID 2>/dev/null
    fi
    
    if [ ! -z "$BACKEND_PID" ]; then
        echo "Stopping backend (PID: $BACKEND_PID)..."
        kill $BACKEND_PID 2>/dev/null
        wait $BACKEND_PID 2>/dev/null
    fi
    
    if [ ! -z "$FRONTEND_PID" ]; then
        echo "Stopping frontend (PID: $FRONTEND_PID)..."
        kill $FRONTEND_PID 2>/dev/null
        wait $FRONTEND_PID 2>/dev/null
    fi
    
    pkill -f "uvicorn backend.main:app" 2>/dev/null
    pkill -f "arq services.llm_worker" 2>/dev/null
    pkill -f "vite.*opensoc" 2>/dev/null
    
    echo "✓ Servers stopped"
    echo ""
    echo "Database and Redis are still running. To stop:"
    echo "  cd docker && docker compose stop postgres redis"
    exit 0
}

trap cleanup INT TERM EXIT

# Start backend API
echo ""
echo "Starting backend API server..."
export PYTHONPATH="${PWD}:${PYTHONPATH}"

uvicorn backend.main:app \
    --host 127.0.0.1 \
    --port 6987 \
    --reload \
    --reload-dir backend \
    --reload-dir services \
    --reload-dir database &

BACKEND_PID=$!
sleep 2

# Start ARQ LLM workers (one per priority queue: triage, investigation, chat, insights)
echo "Starting LLM workers (ARQ)..."
python3 -m services.run_llm_worker &
WORKER_PID=$!
sleep 1

# Start frontend
if [ -d "frontend/node_modules" ]; then
    echo "Starting frontend dev server..."
    cd frontend
    npm run dev &
    FRONTEND_PID=$!
    cd ..
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

