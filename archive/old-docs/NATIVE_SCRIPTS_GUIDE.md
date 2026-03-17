# Linux-Native Startup Scripts Guide

## ✅ Scripts Retrieved & Updated

Your original Linux-native startup scripts have been retrieved from GitHub and updated for AI SOC v2.0!

---

## 📁 Available Scripts

| Script | Purpose | Mode |
|--------|---------|------|
| `start_web.sh` | Start everything interactively | **Interactive** - keeps terminal |
| `start_daemon.sh` | Start everything in background | **Background** - frees terminal |
| `start_database.sh` | Start only PostgreSQL | Database only |
| `shutdown_all.sh` | Stop all services | Cleanup |
| `stop_database.sh` | Stop only database | Database only |

All scripts are now **executable** and ready to use! ✅

---

## 🚀 Quick Start

### Option 1: Interactive Mode (Recommended for Development)

```bash
# Start everything, keeps terminal open
./start_web.sh

# Press Ctrl+C to stop when done
```

**Best for:**
- Active development
- Seeing live logs
- Quick testing
- First-time setup

### Option 2: Background Mode (Daemon)

```bash
# Start everything in background
./start_daemon.sh

# Terminal is free for other work
# Stop with:
./shutdown_all.sh
```

**Best for:**
- Long-running development sessions
- Multi-tasking
- Server-like operation
- Production testing

---

## 📋 What Each Script Does

### `start_web.sh` - Interactive Mode

**Features:**
- ✅ Creates/activates Python venv
- ✅ Installs/updates dependencies
- ✅ Starts PostgreSQL (via Docker)
- ✅ Starts Backend API (port 6987)
- ✅ Starts Frontend dev server (port 6988)
- ✅ Enables hot-reload for code changes
- ✅ Loads DEV_MODE from .env
- ✅ Cleans up on Ctrl+C

**Usage:**
```bash
./start_web.sh
```

**What you'll see:**
```
==========================================
Vigil SOC v2.0 - Startup
==========================================
✓ Loading environment variables from .env
✓ PostgreSQL is already running
✓ Frontend dependencies OK

Starting backend API server...
Starting frontend dev server...

==========================================
✅ Vigil SOC v2.0 - Ready!
==========================================
Backend API:   http://localhost:6987
Frontend UI:   http://localhost:6988
API Docs:      http://localhost:6987/docs

⚠️  DEV_MODE ENABLED - Auth bypassed!

Press Ctrl+C to stop
==========================================
```

---

### `start_daemon.sh` - Background Mode

**Features:**
- ✅ Starts everything in background
- ✅ Creates PID files for tracking
- ✅ Logs to `logs/` directory
- ✅ Starts Backend API
- ✅ Starts SOC Daemon (SIEM poller)
- ✅ Starts Frontend
- ✅ Frees terminal immediately

**Usage:**
```bash
./start_daemon.sh
```

**Logs location:**
```bash
logs/
├── backend.log   # API server logs
├── daemon.log    # SIEM poller logs
└── frontend.log  # Vite dev server logs
```

**View logs:**
```bash
# Watch backend logs
tail -f logs/backend.log

# Watch daemon (SIEM poller) logs
tail -f logs/daemon.log

# Watch frontend logs
tail -f logs/frontend.log

# Watch all logs
tail -f logs/*.log
```

---

### `start_database.sh` - Database Only

**Features:**
- ✅ Starts PostgreSQL via Docker Compose
- ✅ Waits for database to be ready
- ✅ Initializes tables if needed
- ✅ Shows connection info

**Usage:**
```bash
./start_database.sh
```

**Connect to database:**
```bash
docker exec -it deeptempo-postgres psql -U postgres -d deeptempo
```

---

### `shutdown_all.sh` - Complete Shutdown

**Features:**
- ✅ Stops all daemon processes (using PID files)
- ✅ Kills processes by name
- ✅ Kills processes by port (6987, 6988)
- ✅ Stops SOC daemon
- ✅ Stops MCP servers
- ✅ Cleans up zombie processes
- ✅ Shows status report

**Usage:**
```bash
./shutdown_all.sh
```

**What it cleans:**
- Backend API processes
- Frontend dev server
- SOC Daemon (SIEM poller)
- MCP servers
- Any processes on ports 6987, 6988

---

### `stop_database.sh` - Database Only

**Features:**
- ✅ Stops PostgreSQL container
- ✅ Keeps data intact
- ✅ Fast restart possible

**Usage:**
```bash
./stop_database.sh
```

---

## 🔧 Setup & Configuration

### First Time Setup

```bash
# 1. Create environment file
cat > .env << 'EOF'
DEV_MODE=true
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/deeptempo
JWT_SECRET_KEY=dev-secret-key-change-in-production
JWT_ALGORITHM=HS256
JWT_EXPIRATION_MINUTES=1440
LOG_LEVEL=INFO
EOF

# 2. Create frontend environment
cd frontend
cat > .env.development << 'EOF'
VITE_DEV_MODE=true
VITE_API_URL=http://localhost:6987
VITE_ENABLE_ANALYTICS=true
EOF
cd ..

# 3. Start everything!
./start_web.sh
```

---

## 🆚 Native Scripts vs Docker Compose

### Native Scripts (These)

**Pros:**
- ✅ Faster startup (no container building)
- ✅ Direct access to code (easier debugging)
- ✅ Hot-reload works perfectly
- ✅ Lower resource usage
- ✅ More flexible for development
- ✅ Can run components independently

**Cons:**
- ⚠️ Requires Python 3.11+ and Node.js
- ⚠️ Still needs Docker for PostgreSQL
- ⚠️ Slightly more setup

**Best for:** Development, testing, debugging

### Docker Compose

**Pros:**
- ✅ Everything containerized
- ✅ Identical to production
- ✅ No local Python/Node needed
- ✅ Easy to reproduce

**Cons:**
- ⚠️ Slower startup (builds images)
- ⚠️ More resource intensive
- ⚠️ Hot-reload can be slower

**Best for:** Production, CI/CD, consistent environments

---

## 💡 Common Workflows

### Daily Development

```bash
# Morning: Start everything
./start_daemon.sh

# Develop all day (auto-reloads on changes)
# Check logs if needed: tail -f logs/backend.log

# Evening: Stop everything
./shutdown_all.sh
```

### Quick Testing

```bash
# Start and watch logs
./start_web.sh

# Test your changes
# Ctrl+C when done (auto-cleanup)
```

### Database Management

```bash
# Just need database for backend testing
./start_database.sh

# Run backend manually
source venv/bin/activate
export PYTHONPATH=$PWD:$PYTHONPATH
uvicorn backend.main:app --reload

# Stop database when done
./stop_database.sh
```

### Frontend-Only Development

```bash
# Start backend in background
./start_daemon.sh

# Work on frontend with hot reload
cd frontend
npm run dev

# Frontend changes reload instantly
# Backend runs in background

# Stop when done
./shutdown_all.sh
```

---

## 🐛 Troubleshooting

### "Backend already running"

```bash
# Clean everything first
./shutdown_all.sh

# Then start fresh
./start_web.sh
```

### "Virtual environment not found"

```bash
# Create venv
python3 -m venv venv

# Then start
./start_web.sh
```

### "PostgreSQL failed to start"

```bash
# Check Docker
docker ps

# Start database manually
./start_database.sh

# Check logs
cd docker && docker-compose logs postgres
```

### Port already in use

```bash
# Kill processes on ports
lsof -ti:6987 | xargs kill -9
lsof -ti:6988 | xargs kill -9

# Or use shutdown script
./shutdown_all.sh
```

### Can't see logs in daemon mode

```bash
# Logs are in the logs/ directory
tail -f logs/backend.log
tail -f logs/daemon.log
tail -f logs/frontend.log
```

---

## 🔍 What's Running?

### Check Status

```bash
# Check processes
ps aux | grep -E "(uvicorn|daemon|vite)"

# Check ports
lsof -i :6987  # Backend
lsof -i :6988  # Frontend
lsof -i :5432  # PostgreSQL

# Check PIDs (if daemon mode)
cat logs/*.pid

# Check Docker
docker ps
```

---

## 📊 Log Files

When running in daemon mode, logs go to:

```
logs/
├── backend.log    # Backend API
│   - Startup info
│   - API requests
│   - Errors
│   - DEV_MODE warnings
│
├── daemon.log     # SOC Daemon (SIEM poller)
│   - SIEM connections
│   - Finding ingestion
│   - Polling cycles
│   - Errors
│
├── frontend.log   # Vite dev server
│   - Build output
│   - Hot reload events
│   - Errors
│
├── backend.pid    # Backend PID
├── daemon.pid     # Daemon PID
└── frontend.pid   # Frontend PID
```

**Useful log commands:**
```bash
# Watch all logs
tail -f logs/*.log

# Watch backend only
tail -f logs/backend.log

# Search logs
grep -i error logs/*.log
grep -i "dev_mode" logs/backend.log

# Clear logs
> logs/backend.log
> logs/daemon.log
> logs/frontend.log
```

---

## 🎯 Comparison with Docker

| Feature | Native Scripts | Docker Compose |
|---------|---------------|----------------|
| **Startup Time** | ~5 seconds | ~30-60 seconds |
| **Hot Reload** | Instant | 1-2 seconds |
| **Resource Use** | Low | Medium-High |
| **Debugging** | Easy (direct access) | Harder (in containers) |
| **Setup Effort** | Medium | Low |
| **Production-like** | No | Yes |
| **Best For** | Development | Production/CI |

---

## ✅ Summary

You now have **two ways** to run AI SOC:

### 🐧 Native Scripts (This Guide)
```bash
./start_web.sh         # Interactive
./start_daemon.sh      # Background
./shutdown_all.sh      # Stop
```

### 🐳 Docker Compose
```bash
cd docker
docker-compose up -d
docker-compose down
```

**Recommendation:** Use native scripts for day-to-day development, Docker for production testing!

---

## 🎉 You're Ready!

Pick your preferred method and start developing:

```bash
# Quick start (interactive)
./start_web.sh

# Or background mode
./start_daemon.sh
```

Open http://localhost:6988 and start coding! 🚀

---

*Last Updated: January 20, 2026*  
*Version: 2.0.0*

