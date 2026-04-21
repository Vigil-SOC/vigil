#!/bin/bash
# Complete shutdown for Vigil SOC v2.0
# Usage: ./shutdown_all.sh [-d|--docker] [--full]
#   -d, --docker : Also shutdown Docker containers
#   --full       : When used with --docker, removes containers and volumes (clean slate)

DOCKER_SHUTDOWN=false
FULL_CLEANUP=false

# Parse arguments
for arg in "$@"; do
    case $arg in
        -d|--docker)
            DOCKER_SHUTDOWN=true
            ;;
        --full)
            FULL_CLEANUP=true
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: ./shutdown_all.sh [-d|--docker] [--full]"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo "Vigil SOC v2.0 - Complete Shutdown"
if [ "$DOCKER_SHUTDOWN" == "true" ]; then
    echo "(Docker shutdown enabled)"
    if [ "$FULL_CLEANUP" == "true" ]; then
        echo "(Full cleanup mode - will remove containers)"
    fi
else
    echo "(Docker will remain running)"
fi
echo "=========================================="
echo ""

# Stop daemon mode processes
echo "1. Stopping daemon processes..."
if [ -f "logs/backend.pid" ]; then
    BACKEND_PID=$(cat logs/backend.pid)
    kill $BACKEND_PID 2>/dev/null && echo "   ✓ Backend stopped (PID: $BACKEND_PID)" || echo "   Backend already stopped"
    rm logs/backend.pid
fi

if [ -f "logs/daemon.pid" ]; then
    DAEMON_PID=$(cat logs/daemon.pid)
    kill $DAEMON_PID 2>/dev/null && echo "   ✓ SOC Daemon stopped (PID: $DAEMON_PID)" || echo "   Daemon already stopped"
    rm logs/daemon.pid
fi

if [ -f "logs/frontend.pid" ]; then
    FRONTEND_PID=$(cat logs/frontend.pid)
    kill $FRONTEND_PID 2>/dev/null && echo "   ✓ Frontend stopped (PID: $FRONTEND_PID)" || echo "   Frontend already stopped"
    rm logs/frontend.pid
fi

if [ -f "logs/llm_worker.pid" ]; then
    LLM_WORKER_PID=$(cat logs/llm_worker.pid)
    kill $LLM_WORKER_PID 2>/dev/null && echo "   ✓ LLM Worker stopped (PID: $LLM_WORKER_PID)" || echo "   LLM Worker already stopped"
    rm logs/llm_worker.pid
fi

# Kill processes by name
echo "2. Stopping backend processes..."
pkill -f "uvicorn backend.main:app"
pkill -f "start_web.sh"

# Kill by port
echo "3. Stopping processes on ports..."
lsof -ti:6987 | xargs kill -9 2>/dev/null || echo "   No processes on port 6987"
lsof -ti:6988 | xargs kill -9 2>/dev/null || echo "   No processes on port 6988"

# Kill daemon
echo "4. Stopping daemon processes..."
pkill -f "daemon.main"
pkill -f "daemon/main.py"

# Kill MCP servers
echo "5. Stopping MCP servers..."
pkill -f "mcp_servers.*_server"
ps aux | grep -i "mcp_servers" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null || echo "   No MCP processes"

# Kill frontend
echo "6. Stopping frontend..."
pkill -f "vite.*opensoc"

# Clean zombies
echo "7. Cleaning zombies..."
ps aux | grep defunct | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null || echo "   No zombies"

# Stop Docker services (only if --docker flag is passed)
if [ "$DOCKER_SHUTDOWN" == "true" ]; then
    echo "8. Stopping Docker containers..."
    if command -v docker &> /dev/null; then
        if [ -d "docker" ]; then
            cd docker
            
            # Check which compose command to use
            if command -v docker-compose &> /dev/null; then
                DOCKER_COMPOSE="docker-compose"
            else
                DOCKER_COMPOSE="docker compose"
            fi
            
            # Stop all services
            if [ "$FULL_CLEANUP" == "true" ]; then
                echo "   Stopping and removing Docker containers..."
                $DOCKER_COMPOSE down 2>/dev/null || echo "   No Docker services running"
                echo "   ✓ Docker containers removed"
                
                # Ask about volumes
                read -p "   Also remove database volumes (⚠️  deletes all data)? (y/N) " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    $DOCKER_COMPOSE down -v 2>/dev/null
                    echo "   ✓ Docker volumes removed (database wiped)"
                else
                    echo "   ℹ️  Database volumes kept"
                fi
            else
                echo "   Stopping Docker services..."
                $DOCKER_COMPOSE stop 2>/dev/null || echo "   No Docker services running"
                echo "   ✓ Docker services stopped (containers kept)"
                echo "   ℹ️  For full cleanup: ./shutdown_all.sh --docker --full"
            fi
            
            cd ..
        else
            echo "   ⚠️  docker/ directory not found"
        fi
    else
        echo "   ⚠️  Docker not installed, skipping"
    fi
else
    echo "8. Skipping Docker shutdown (use -d or --docker to shutdown Docker)"
fi

echo ""
echo "=========================================="
echo "Status Check"
echo "=========================================="

REMAINING=$(ps aux | grep -E "(uvicorn|daemon.main|mcp_servers|start_web)" | grep -v grep | grep -v shutdown_all)

if [ -z "$REMAINING" ]; then
    echo "✅ All processes stopped!"
else
    echo "⚠️  Some processes still running:"
    echo "$REMAINING"
fi

echo ""
echo "Port status:"
echo "  6987 (Backend):  $(lsof -ti:6987 | wc -l | xargs) process(es)"
echo "  6988 (Frontend): $(lsof -ti:6988 | wc -l | xargs) process(es)"

# Check Docker status
if command -v docker &> /dev/null && [ -d "docker" ]; then
    echo ""
    if [ "$DOCKER_SHUTDOWN" == "true" ]; then
        echo "Docker status (after shutdown):"
    else
        echo "Docker status (not modified):"
    fi
    cd docker
    if command -v docker-compose &> /dev/null; then
        DOCKER_COMPOSE="docker-compose"
    else
        DOCKER_COMPOSE="docker compose"
    fi
    $DOCKER_COMPOSE ps --format table 2>/dev/null || echo "  No Docker services"
    cd ..
fi

echo ""
echo "=========================================="
echo "✅ Complete Cleanup Done!"
echo "=========================================="
echo ""
if [ "$DOCKER_SHUTDOWN" == "true" ]; then
    if [ "$FULL_CLEANUP" == "true" ]; then
        echo "Everything stopped and removed (native processes + Docker containers)"
    else
        echo "Everything stopped (native processes + Docker containers)"
    fi
else
    echo "Native processes stopped (Docker left running)"
fi
echo ""
echo "To restart:"
echo "  ./start_web.sh          - Interactive (keeps terminal)"
echo "  ./start_daemon.sh       - Background (frees terminal)"
echo ""
echo "Shutdown options:"
echo "  ./shutdown_all.sh              - Stop native processes only (current)"
echo "  ./shutdown_all.sh -d           - Stop native processes + Docker"
echo "  ./shutdown_all.sh -d --full    - Stop + remove containers and volumes"
echo ""

