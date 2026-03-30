# CLAUDE.md — Vigil SOC

This file provides guidance for AI assistants (Claude Code and similar tools) working in this repository.

---

## Project Overview

**Vigil** is an open-source, AI-native Security Operations Center (SOC) platform. It orchestrates 12 specialized AI agents via Claude to perform triage, investigation, threat hunting, forensics, and automated response across 30+ security integrations.

**Core pillars:**
- **Agents** — 12 specialized AI agents (Triage, Investigator, Threat Hunter, Correlator, Responder, Reporter, MITRE Analyst, Forensics, Threat Intel, Compliance, Malware Analyst, Network Analyst)
- **Workflows** — Multi-agent orchestrated playbooks (Incident Response, Full Investigation, Threat Hunt, Forensic Analysis)
- **Integrations** — 30+ tools via MCP protocol (Splunk, CrowdStrike, VirusTotal, Shodan, Timesketch, Jira, Slack, etc.)

**Ports:**
- Backend API: `http://localhost:6987`
- Frontend: `http://localhost:6988`
- PostgreSQL: `5432`
- Redis: `6379`

---

## Repository Structure

```
vigil/
├── backend/              # FastAPI REST API
│   ├── main.py           # App entry point, router registration
│   ├── api/              # 27 route modules (findings, cases, claude, auth, etc.)
│   ├── middleware/       # Auth middleware
│   └── schemas/          # Pydantic request/response schemas
├── services/             # 40+ business logic service classes
│   ├── claude_service.py # Central AI orchestration (largest file ~124KB)
│   ├── soc_agents.py     # Agent prompt definitions
│   ├── mcp_service.py    # MCP server coordination
│   └── case_*_service.py # Case lifecycle services
├── daemon/               # Autonomous 24/7 SOC background process
│   ├── orchestrator.py   # Main autonomous agent orchestrator
│   ├── agent_runner.py   # Executes agents with cost/resource guardrails
│   ├── poller.py         # Fetches alerts from SIEM/EDR
│   ├── processor.py      # Processes findings through AI pipeline
│   ├── responder.py      # Executes containment actions
│   └── scheduler.py      # Cron-style scheduled tasks
├── frontend/             # React + TypeScript + Vite SPA
│   └── src/
│       ├── pages/        # Route-level page components
│       ├── components/   # Feature components (16 subdirectories)
│       ├── services/     # Axios API client services
│       ├── contexts/     # React Context (auth, theme)
│       └── theme/        # MUI customization
├── workflows/            # Workflow definitions as WORKFLOW.md files
│   ├── incident-response/WORKFLOW.md
│   ├── full-investigation/WORKFLOW.md
│   ├── threat-hunt/WORKFLOW.md
│   └── forensic-analysis/WORKFLOW.md
├── tools/                # MCP tool implementations (15+ integrations)
├── mcp-servers/          # Git submodule: MCP server implementations
├── deeptempo-core/       # Git submodule: core AI/detection library
├── database/
│   └── init/             # PostgreSQL init SQL (runs in order 01-06)
├── core/                 # Config, secrets management, rate limiting
├── data/                 # Schemas, MITRE taxonomy, detection registry
├── tests/                # pytest + vitest test suites
├── docs/                 # Detailed documentation
├── docker/               # docker-compose.yml + Dockerfiles
├── scripts/              # Init and utility shell scripts
├── mcp-config.json       # 30+ MCP server definitions
└── env.example           # Template for all 220+ environment variables
```

---

## Development Setup

### Quick Start

```bash
git clone --recurse-submodules https://github.com/Vigil-SOC/vigil.git
cd vigil
./start_web.sh       # Starts PostgreSQL (Docker), backend, and frontend
```

### Manual Start

```bash
# 1. Start infrastructure
cd docker && docker compose up -d postgres redis

# 2. Backend (from repo root)
source venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 6987 --reload

# 3. Frontend
cd frontend && npm run dev

# 4. (Optional) Daemon
./start_daemon.sh
```

### Fresh Environment

```bash
./setup_dev.sh   # Creates venv, installs all Python + npm deps
```

### Prerequisites

- **Python 3.10+** (required by claude-agent-sdk)
- **Node.js 18+**
- **Docker Desktop** (must be running — used for PostgreSQL and Redis)
- **Git** with submodule support

### Environment Variables

Copy `env.example` to `.env` and populate as needed. Key variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DEV_MODE` | Bypass all authentication | `true` |
| `ANTHROPIC_API_KEY` | Claude API access | required for AI features |
| `DATABASE_URL` | PostgreSQL connection | auto-set by docker-compose |
| `REDIS_URL` | ARQ job queue | `redis://localhost:6379/0` |
| `SPLUNK_URL` / credentials | Splunk SIEM | optional |
| `CROWDSTRIKE_CLIENT_ID/SECRET` | CrowdStrike EDR | optional |
| `VIRUSTOTAL_API_KEY` | Threat intel | optional |

Default dev login: **admin / admin123** (when `DEV_MODE=false`)

---

## Running Tests

### Python (pytest)

```bash
# All tests
pytest

# With coverage
pytest --cov=. --cov-report=html

# By marker
pytest -m unit
pytest -m integration   # requires running PostgreSQL
pytest -m "not slow"

# Specific file
pytest tests/test_backend_tools.py -v
```

Available markers: `unit`, `integration`, `slow`, `auth`, `siem`, `claude`, `database`, `api`, `daemon`, `performance`

### Frontend (vitest)

```bash
cd frontend
npm run test           # watch mode
npm run test:run       # single run with verbose output
npm run test:coverage  # with coverage report
npm run test:ci        # CI mode (JSON output)
```

### Linting

```bash
# Python
black .                # format (line length 88)
flake8 .               # lint (max line length 88)
isort .                # sort imports
mypy . --ignore-missing-imports

# TypeScript
cd frontend && npm run lint

# Pre-commit (runs black automatically)
pre-commit run --all-files
```

---

## Key Architecture Patterns

### Async-First Backend

All FastAPI endpoints and service methods use `async/await`. Long-running LLM operations go through the ARQ Redis queue (worker pattern). Never add blocking I/O to endpoint handlers.

### Service Layer

Business logic lives in `services/`, not in API route handlers. Route handlers in `backend/api/` should delegate to service classes. When adding a feature:
1. Add logic to an existing service or create `services/your_feature_service.py`
2. Add the route in `backend/api/your_feature.py`
3. Register the router in `backend/main.py`

### MCP Tool Access

Agents access external tools through the MCP protocol. Tool definitions live in `mcp-config.json`. New integrations go in `tools/` as MCP server implementations. The `services/mcp_service.py` coordinates tool access.

### Database

- PostgreSQL 16 via SQLAlchemy ORM (`services/models.py`)
- Schema initialized by `database/init/` SQL files (executed in numeric order)
- pgvector extension for embeddings
- Use `services/database_data_service.py` for data access — do not query the DB directly from API handlers

### Authentication

- `DEV_MODE=true` (default) bypasses all auth — use for local development
- Production uses JWT tokens via `backend/api/auth.py` + `backend/middleware/`
- RBAC is implemented in `database/init/06_auth_tables.sql`

### Daemon / Autonomous Mode

The daemon (`daemon/`) runs as a separate process with its own orchestration loop. It polls for new alerts, processes them through the AI pipeline, and can execute automated responses. Cost and resource guardrails are enforced by `daemon/agent_runner.py`.

Key config variables: `DAEMON_AUTO_TRIAGE`, `DAEMON_CONFIDENCE_THRESHOLD`, `ORCHESTRATOR_MAX_COST`, `ORCHESTRATOR_MAX_HOURLY_COST`

---

## Code Conventions

### Python

- **Formatter**: Black, line length **88**
- **Linter**: Flake8, max line length 88
- **Imports**: isort
- **Type hints**: mypy (ignore-missing-imports mode)
- **Async**: prefer `async def` for all service methods and route handlers
- File naming: `snake_case.py`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`

### TypeScript / React

- **Framework**: React 18 + Vite 5 (not CRA)
- **UI**: Material-UI (MUI) v5 — use MUI components, not custom CSS primitives
- **State/data**: `@tanstack/react-query` for server state; React Context for auth/theme
- **HTTP**: axios via `frontend/src/services/`
- **Linter**: ESLint with `@typescript-eslint/recommended` + `react-hooks/recommended`
- Component files: `PascalCase.tsx`
- Service files: `camelCase.ts`
- Test files collocated: `Component.test.tsx`

### Commit Messages

- Required: DCO sign-off (`git commit -s`)
- Format: short summary line (≤50 chars) + optional body (wrap at 72)
- One logical change per commit
- Example: `git commit -s -m "Add SentinelOne MCP integration"`

### API Routes

Follow the existing pattern:
```python
# backend/api/your_feature.py
router = APIRouter(prefix="/api/your-feature", tags=["your-feature"])

@router.get("/")
async def list_items(db: AsyncSession = Depends(get_db)):
    return await your_feature_service.list(db)
```
Register in `backend/main.py`.

---

## Adding New Features

### New MCP Integration

1. Implement the MCP server in `tools/your_tool.py` or `mcp-servers/`
2. Add the server definition to `mcp-config.json`
3. Expose via `services/mcp_service.py` if needed
4. Document in `docs/INTEGRATIONS.md`

### New Agent

1. Add prompt definition in `services/soc_agents.py`
2. Wire agent invocation in `services/claude_service.py`
3. Expose via `backend/api/agents.py`
4. Document in `docs/AGENTS.md`

### New Workflow

1. Create `workflows/your-workflow/WORKFLOW.md` following existing format
2. Define agent sequence, tools, and phase instructions
3. Register in workflow service if needed

### New API Endpoint

1. Add route handler in `backend/api/` (new file or extend existing)
2. Add service logic in `services/`
3. Add Pydantic schema in `backend/schemas/` if needed
4. Register router in `backend/main.py`
5. Add corresponding frontend API call in `frontend/src/services/`

---

## CI/CD

GitHub Actions workflows in `.github/workflows/`:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci-cd.yml` | Push/PR to main, develop | Lint → Unit Tests → Integration Tests → Security Scan → Docker Build |
| `release.yml` | Version tags (`v*`) | Release management |
| `nightly.yml` | Daily 2 AM UTC | Comprehensive security & performance audits |

CI runs:
- `black --check` + `flake8` + `isort --check` + `mypy` (Python)
- `eslint` (TypeScript)
- `hadolint` (Dockerfiles)
- `pytest --cov` (backend unit + integration)
- `vitest run` (frontend)
- `bandit` + `npm audit` (security)

All CI checks must pass before merging.

---

## Important Files

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app, all router registrations |
| `services/claude_service.py` | Central AI/agent orchestration (~124KB) |
| `services/soc_agents.py` | All agent system prompts |
| `services/mcp_service.py` | MCP protocol coordination |
| `database/init/` | Schema SQL (01–06, applied in order) |
| `mcp-config.json` | All MCP server definitions |
| `env.example` | Every supported environment variable |
| `docker/docker-compose.yml` | Full local stack definition |
| `docs/AGENTS.md` | Agent reference |
| `docs/INTEGRATIONS.md` | Integration/MCP reference |
| `DEV_MODE.md` | Development auth bypass details |

---

## Submodules

This repo uses two Git submodules:

```bash
# Initialize after cloning
git submodule update --init --recursive

# Update submodules
git submodule update --remote
```

| Submodule | Path | Purpose |
|-----------|------|---------|
| `deeptempo-core` | `./deeptempo-core` | Core AI and detection library |
| `mcp-servers` | `./mcp-servers` | MCP server implementations |

Both are installed as editable packages (`-e ./deeptempo-core`, `-e ./mcp-servers`) in `requirements.txt`. If submodules aren't initialized, `start_web.sh` skips their installation gracefully.

---

## Security Notes

- Never commit secrets or API keys — use `.env` (gitignored)
- `DEV_MODE=true` disables all auth — **never enable in production**
- Default PostgreSQL password in `docker-compose.yml` must be changed for production
- Bandit runs in CI to catch common Python security issues
- MCP tool calls that perform actions (host isolation, firewall rules) require approval workflow by default — see `services/approval_service.py`
