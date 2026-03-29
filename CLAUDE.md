# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Vigil is an AI-Native Security Operations Center (SOC) built on Claude with a modular multi-agent architecture. It orchestrates 13 specialized AI agents across 4 multi-agent workflows ("Skills"), with integrations to 30+ security tools.

## Commands

### Starting the Application
```bash
./start_web.sh                  # Start everything (backend + frontend + docker)
./scripts/start_daemon.sh       # Start headless autonomous SOC daemon
./scripts/shutdown_all.sh       # Stop services (keep Docker)
./scripts/shutdown_all.sh -d    # Stop services + Docker containers
```

### Manual Start (Multi-Terminal)
```bash
# Terminal 1 - Database
cd docker && docker compose up -d postgres

# Terminal 2 - Backend (from repo root)
source venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
uvicorn backend.main:app --host 127.0.0.1 --port 6987 --reload

# Terminal 3 - Frontend
cd frontend && npm run dev
```

### Testing
```bash
./scripts/run-tests.sh          # Full test suite (pytest + frontend)
pytest tests/ -v                # Python tests directly
pytest tests/unit/ -v           # Unit tests only
cd frontend && npm test          # Frontend tests (watch mode)
cd frontend && npm run test:run  # Frontend tests (single run)
cd frontend && npm run test:coverage
```

### Frontend Build & Lint
```bash
cd frontend && npm run build
cd frontend && npm run lint
```

### Database Migrations
```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Other Scripts (all in `scripts/`)
```bash
python scripts/init_default_user.py   # Create admin user (run once on first setup)
./scripts/setup_dev.sh                # First-time dev environment setup
python scripts/demo.py                # Seed demo data
```

## Access Points (Development)
- Frontend: http://localhost:6988
- Backend API: http://localhost:6987
- API Docs: http://localhost:6987/docs
- Default credentials: `admin` / `admin123`

## Development Mode (Auth Bypass)
Set `DEV_MODE=true` in `.env` to skip authentication entirely. See `DEV_MODE.md` for details. Never use in production.

## Architecture

### Stack
- **Frontend**: React, TypeScript, Vite, Material-UI — in `frontend/`
- **Backend**: FastAPI (Python 3.10+), Uvicorn — in `backend/`
- **Daemon**: Headless autonomous SOC service — in `daemon/`
- **Database**: PostgreSQL 16 + pgvector (embeddings) — in `database/`
- **Job Queue**: Redis + ARQ for LLM request queuing
- **Agent Layer**: Claude Agent SDK (multiple backend tools registered in `backend/api/claude.py`)
- **Integrations**: MCP servers (multiple tools) configured in `mcp-config.json`

### Request Flow
```
Browser (6988) → React UI → FastAPI (6987) → Claude Agent SDK → MCP Servers → External Tools
                                           → PostgreSQL (findings, cases, embeddings, approvals)
```

### Key Modules

**Backend** (`backend/api/`): Each file is a FastAPI router. Key ones:
- `claude.py` — Chat interface and Agent SDK tool registration (the core)
- `findings.py` — Finding CRUD, search, embeddings
- `cases.py` — Case management, timelines, metrics
- `skills.py` — Workflow execution and discovery
- `ai_decisions.py` — Approval workflow for autonomous agent actions
- `orchestrator.py` — Autonomous multi-agent orchestration

**Database** (`database/`):
- `models.py` — All SQLAlchemy ORM models (large file, ~75K)
- `service.py` — Database service layer
- `config_service.py` — Persists configuration to DB

**Daemon** (`daemon/`):
- `orchestrator.py` — Multi-agent orchestration engine
- `agent_runner.py` — Individual agent execution
- `poller.py` — Polls Splunk, CrowdStrike for new findings
- `responder.py` — Autonomous response actions

### Skills (Multi-Agent Workflows)
Skills live in `skills/<name>/SKILL.md`. Each is a markdown file with YAML frontmatter defining agent sequence and phase-by-phase instructions. The 4 built-in skills:
- `incident-response/` — Triage → Investigator → Responder → Reporter
- `full-investigation/` — Investigator → MITRE Analyst → Correlator → Responder → Reporter
- `threat-hunt/` — Threat Hunter → Network Analyst → Malware Analyst → Threat Intel → Reporter
- `forensic-analysis/` — Forensics → Malware Analyst → Network Analyst → Reporter

To add a custom skill: create `skills/my-skill/SKILL.md` with YAML frontmatter, then call `POST /api/skills/reload`.

### Adding a New API Endpoint
1. Create or add to a router file in `backend/api/`
2. Import and register the router in `backend/main.py`

### Adding a New Agent Tool
Define and register the tool in `backend/api/claude.py` where the Agent SDK tools are registered.

### MCP Integration
Add new server config to `mcp-config.json`. Set required env vars. Restart backend.

## Configuration
Copy `env.example` to `.env`. Key variables:
- `ANTHROPIC_API_KEY` — Required for all Claude functionality
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis for job queue
- `DEV_MODE` — Bypass auth (development only)
- `DAEMON_AUTO_TRIAGE`, `DAEMON_AUTO_RESPONSE`, `DAEMON_CONFIDENCE_THRESHOLD` — Autonomous behavior controls
- `ORCHESTRATOR_MAX_COST`, `ORCHESTRATOR_MAX_DAILY_COST` — Cost guardrails

See `docs/CONFIGURATION.md` for complete reference.

## Git Submodules
- `mcp-servers/` — MCP server implementations
- `deeptempo-core/` — Core library

Run `git submodule update --init --recursive` if these directories are empty.

## Documentation
Comprehensive docs in `docs/`: `ARCHITECTURE.md`, `AGENTS.md`, `INTEGRATIONS.md`, `BACKEND_TOOLS.md`, `TESTING_GUIDE.md`, `DEPLOYMENT_GUIDE.md`.
