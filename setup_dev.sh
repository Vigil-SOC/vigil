#!/bin/bash
# =============================================================================
# AI SOC - Quick Development Setup
# =============================================================================
# This script sets up the environment for local development with auth bypass.
# Perfect for fresh clones! Just run: ./setup_dev.sh

set -e  # Exit on error

echo "=========================================="
echo "🚀 AI SOC - Development Setup"
echo "=========================================="
echo ""

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

# Check additional prerequisites (warn but don't fail, so user sees all issues at once)
PREREQ_WARNINGS=0

if ! "$PYTHON" -c "import venv" &> /dev/null; then
    echo "⚠️  python3-venv is not installed."
    echo "   Fix: sudo apt install python3-venv python3-dev"
    PREREQ_WARNINGS=$((PREREQ_WARNINGS + 1))
fi

if ! command -v docker &> /dev/null; then
    echo "⚠️  Docker is not installed (required for PostgreSQL and Redis)."
    echo "   Install from: https://docs.docker.com/engine/install/"
    PREREQ_WARNINGS=$((PREREQ_WARNINGS + 1))
fi

if ! command -v node &> /dev/null; then
    echo "⚠️  Node.js is not installed (required for frontend)."
    echo "   Install from: https://nodejs.org/ or via your package manager."
    PREREQ_WARNINGS=$((PREREQ_WARNINGS + 1))
elif ! node -e "process.exit(parseInt(process.version.slice(1)) >= 18 ? 0 : 1)" 2>/dev/null; then
    echo "⚠️  Node.js 18+ is required. Found: $(node --version)"
    echo "   Install from: https://nodejs.org/"
    PREREQ_WARNINGS=$((PREREQ_WARNINGS + 1))
fi

if ! command -v npm &> /dev/null; then
    echo "⚠️  npm is not installed (required for frontend)."
    echo "   Install Node.js from: https://nodejs.org/"
    PREREQ_WARNINGS=$((PREREQ_WARNINGS + 1))
fi

if [ "$PREREQ_WARNINGS" -gt 0 ]; then
    echo ""
    echo "⚠️  $PREREQ_WARNINGS prerequisite(s) missing. Setup will continue but may fail."
    echo "   Install the missing tools above and re-run this script."
    echo ""
fi

# Step 1: Copy environment files
echo "📝 Setting up environment files..."
if [ ! -f .env ]; then
    cp env.example .env
    echo "✅ Created .env (with DEV_MODE=true)"
else
    echo "ℹ️  .env already exists, skipping..."
fi

echo "ℹ️  Frontend will automatically use DEV_MODE from root .env"
echo ""

# Step 2: Check Python venv
echo "🐍 Checking Python environment..."
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    "$PYTHON" -m venv venv
    echo "✅ Virtual environment created"
else
    echo "✅ Virtual environment exists"
fi

# Step 3: Install Python dependencies
echo ""
echo "📦 Installing Python dependencies..."
source venv/bin/activate
pip install -q --upgrade pip

# Build a filtered requirements file, skipping submodule editable installs
# whose directories aren't yet initialized (missing setup.py / pyproject.toml)
_REQS=$(mktemp)
while IFS= read -r line; do
    if [[ "$line" =~ ^-e[[:space:]]+\. ]]; then
        dir="${line#*-e }"
        dir="${dir#*-e	}"   # handle tab separator
        if [ -f "$dir/setup.py" ] || [ -f "$dir/pyproject.toml" ]; then
            echo "$line"
        fi
    else
        echo "$line"
    fi
done < requirements.txt > "$_REQS"

if pip install -q -r "$_REQS"; then
    echo "✅ Python dependencies installed"
else
    echo "⚠️  Some packages failed to install. Core functionality should work."
fi
rm -f "$_REQS"

# Step 4: Install frontend dependencies
echo ""
echo "📦 Installing frontend dependencies..."
cd frontend
if [ ! -d "node_modules" ]; then
    npm install
    echo "✅ Frontend dependencies installed"
else
    echo "✅ Frontend dependencies already installed"
fi
cd ..

# Step 5: Start database
echo ""
echo "🗄️  Starting PostgreSQL database..."
if command -v docker &> /dev/null; then
    ./scripts/start_database.sh
    echo "✅ Database started"
else
    echo "⚠️  Docker not found. Please install Docker and run: ./scripts/start_database.sh"
fi

# Step 5.5: Setup detection rule repositories
echo ""
if [ "${SKIP_DETECTION_REPOS}" != "true" ]; then
    echo "🔍 Setting up Security Detection Repositories..."
    echo "   (This provides 7,200+ detection rules for coverage analysis)"
    echo "   (Takes 5-10 minutes on first run, ~4GB download)"
    echo ""
    
    if [ ! -d "$HOME/security-detections" ]; then
        echo "📥 Cloning detection repositories to ~/security-detections/..."
        ./scripts/setup_detection_repos.sh
        echo "✅ Detection repositories installed"
    else
        echo "✅ Detection repositories already exist"
        echo "   To update: ./scripts/setup_detection_repos.sh --update"
    fi
else
    echo "⏭️  Skipping detection repositories (SKIP_DETECTION_REPOS=true)"
fi

# Step 5.6: Initialize MemPalace persistent memory palace
echo ""
echo "🏛️  Initializing MemPalace persistent memory..."
MEMPALACE_PALACE_PATH="${MEMPALACE_PALACE_PATH:-$HOME/.vigil/mempalace/palace}"
export MEMPALACE_PALACE_PATH
if python3 -c "import mempalace" 2>/dev/null; then
    # Seed the palace with a status check — this initializes the ChromaDB collection
    python3 -c "
from mempalace.mcp_server import _get_collection
coll = _get_collection()
print('MemPalace ChromaDB collection ready:', coll.count(), 'drawers')
" 2>/dev/null && echo "✅ MemPalace palace initialized at $MEMPALACE_PALACE_PATH" || \
    echo "✅ MemPalace installed (palace will initialize on first use)"
else
    echo "⚠️  mempalace not found. Run: pip install mempalace chromadb"
    echo "   The palace initializes automatically on first agent use."
fi

echo ""
echo "=========================================="
echo "✅ Setup Complete!"
echo "=========================================="
echo ""
echo "🎉 Your dev environment is ready!"
echo ""
echo "⚡ DEV_MODE is ENABLED - Authentication bypassed"
echo "   No login required! Perfect for rapid development."
echo ""
echo "🚀 Start the application:"
echo "   ./start_web.sh          - Interactive mode (keeps terminal open)"
echo "   ./start_daemon.sh       - Background mode (frees terminal)"
echo ""
echo "🌐 Access points:"
echo "   Frontend: http://localhost:6988"
echo "   Backend:  http://localhost:6987"
echo "   API Docs: http://localhost:6987/docs"
echo ""
echo "📚 Learn more:"
echo "   DEV_MODE.md       - Auth bypass details"
echo "   QUICKSTART.md     - Full quick start guide"
echo ""
echo "🛑 Stop services:"
echo "   ./shutdown_all.sh"
echo ""
echo "=========================================="

