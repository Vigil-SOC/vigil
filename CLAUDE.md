# CLAUDE.md — Vigil SOC

This file provides guidance for AI assistants (Claude Code and similar tools) working in this repository.

---

## Project Overview

**Vigil** is an open-source, AI-native Security Operations Center (SOC) platform. It orchestrates 13 specialized AI agents via Claude to perform triage, investigation, threat hunting, forensics, and automated response across 40 security integrations.

**Core pillars:**
- **Agents** — 13 agents defined in `AGENT_CONFIGS`, which is the authoritative
  list: triage, investigator, threat_hunter, correlator, responder, reporter,
  mitre_analyst, forensics, threat_intel, compliance, malware_analyst,
  network_analyst, auto_responder. (The README says "12" — it omits
  `auto_responder`.)
- **Workflows** — Multi-agent orchestrated playbooks (Incident Response, Full Investigation, Threat Hunt, Forensic Analysis)
- **Integrations** — 40 MCP servers in `mcp-config.json` (Splunk, CrowdStrike, VirusTotal, Shodan, Timesketch, Jira, Slack, etc.). Count only dict-valued keys: the `mcpServers` object also holds 7 `_comment_*` string keys used as section separators.

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
│   ├── api/              # 38 route modules (findings, cases, claude, auth, etc.)
│   ├── middleware/       # Auth middleware
│   └── schemas/          # Pydantic request/response schemas
├── services/             # 70+ business logic service classes
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
│       ├── redesign/     # The SOC console — screens/, shell/, shared/
│       ├── components/   # Cross-console components (auth, setup)
│       ├── services/     # Axios API client services
│       └── contexts/     # React Context (auth, theme)
├── workflows/            # Workflow definitions as WORKFLOW.md files
│   ├── incident-response/WORKFLOW.md
│   ├── full-investigation/WORKFLOW.md
│   ├── threat-hunt/WORKFLOW.md
│   └── forensic-analysis/WORKFLOW.md
├── tools/                # MCP tool implementations (15+ integrations)
├── mcp-servers/          # Git submodule: MCP server implementations
├── deeptempo-core/       # Git submodule: core AI/detection library
├── database/
│   └── init/             # PostgreSQL init SQL (docker-compose: lex order by filename; Helm: values.yaml dbInit.sqlFiles)
├── core/                 # Config, secrets management, rate limiting
├── data/                 # Schemas, MITRE taxonomy, detection registry
├── tests/                # pytest + vitest test suites
├── docs/                 # Detailed documentation
├── docker/               # docker-compose.yml + Dockerfiles
├── scripts/              # Init and utility shell scripts
├── mcp-config.json       # 40 MCP server definitions (+ `_comment_*` separator keys)
└── env.example           # Template for all 220+ environment variables
```

> **sys.path quirk:** `backend/main.py` puts `backend/` on `sys.path`, so modules
> there are imported *bare* — `from secrets_manager import get_secret`, not
> `from backend.secrets_manager import …`. `setup.cfg` (`mypy_path = backend`) and
> `pyrightconfig.json` (`extraPaths`) mirror this so static analysis resolves them.
> Note the resulting name collisions: `backend/services/` and `backend/tools/` are
> **not** the top-level `services/` and `tools/` packages.

---

## Development Setup

### Quick Start

```bash
git clone --recurse-submodules https://github.com/Vigil-SOC/vigil.git
cd vigil
./start.sh           # Starts PostgreSQL (Docker), backend, and frontend
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
./start.sh --daemon
```

### Desktop (Electron)

```bash
cd desktop && npm run dev    # builds TS, launches Electron
cd desktop && npm run dist   # packages a .dmg via electron-builder
```

The desktop app drives the stack through `scripts/app_up.sh` / `app_down.sh`
rather than `start.sh`: no `--reload`, no Vite (the built SPA is served by the
backend at :6987), and it **forces `DEV_MODE=false`** because its login and
first-run bootstrap are the point.

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

Copy `env.example` to `.env` and populate as needed. `.env` is for
bootstrap-only settings (DB URL, ports, dev flags). LLM provider keys,
integration credentials, and other secrets are configured in the web UI
(Settings → AI / LLM Providers, Settings → Integrations) and stored
encrypted at `~/.vigil/secrets.enc` — see [docs/STATE.md](docs/STATE.md).

| Variable | Purpose | Default |
|----------|---------|---------|
| `DEV_MODE` | Bypass all authentication | `false` when unset; `env.example` ships `true` |
| `DATABASE_URL` | PostgreSQL connection | auto-set by docker-compose |
| `REDIS_URL` | ARQ job queue | `redis://localhost:6379/0` |
| `BIFROST_URL` | LLM gateway address | `http://bifrost:8080` |
| `SECRETS_BACKEND` | Where new secrets are written | `encrypted` |

> ⚠️ Do **not** put `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, Splunk
> credentials, etc. in `.env`. Use the UI. Placeholder values in `.env`
> are ignored when the encrypted store has a value.

Default dev login: **admin / admin123** (when `DEV_MODE=false`)

### Reading configuration

Config flows through exactly three channels. Do not add `os.getenv` calls —
`tests/unit/test_no_ambient_state.py` fails CI on them.

| Need | Use | Owner |
|------|-----|-------|
| Non-secret setting | `core.config.get_settings().field` | env / `.env` |
| Credential | `core.secrets.get_secret("NAME")` | `~/.vigil/secrets.enc`, then env |
| UI-editable at runtime | `services.runtime_config` / `database.config_service` | `system_config` table |

`core/config.py` is the single definition site: every setting is a typed field
with its default, and `tests/unit/test_settings_env_example.py` fails if a field
is missing from `env.example` or vice versa. `get_settings()` is `lru_cache`d, so
a test that changes env mid-test must call `get_settings.cache_clear()`.

Config file paths go through `core.config.vigil_path()`, which reads from
`~/.vigil` with a fallback to the legacy `~/.deeptempo` copy and always writes to
`~/.vigil`. Pass `write=True` on save paths.

The two legitimate exceptions, both marked `# noqa: ENV001`: exporting env into
spawned MCP child processes (env is their config protocol, so third-party servers
need no adaptation), and genuinely dynamic variable names.

---

## Running Tests

### Python (pytest)

The pytest config lives at **`tests/pytest.ini`** — there is no root-level
`pytest.ini` and no `[tool:pytest]` in `setup.cfg`. A bare `pytest` from the repo
root therefore finds no ini file: markers go unregistered, `--asyncio-mode=auto`
and the coverage flags are skipped, and collection starts from the CWD instead of
`testpaths = tests`. Always scope to `tests/` (or pass `-c tests/pytest.ini`).

```bash
# All tests, with the intended config
pytest tests/

# By marker
pytest tests/ -m unit
pytest tests/ -m integration   # requires running PostgreSQL
pytest tests/ -m "not external_service"   # what CI's main unit job runs

# Specific file
pytest tests/unit/test_backend_tools.py -v
```

Available markers: `unit`, `integration`, `slow`, `auth`, `siem`, `claude`,
`database`, `api`, `daemon`, `performance`, `external_service`.
`--strict-markers` is on, so an unregistered marker is an error — add new ones to
`tests/pytest.ini`.

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

**The DB layer is synchronous SQLAlchemy — there is no `AsyncSession` in this
repo.** `database/connection.py` exposes a `sessionmaker` and a `get_db()`
dependency yielding a plain `Session`. So don't type a dependency as
`AsyncSession` or `await` a session call. Handlers that are fully synchronous can
be plain `def` (FastAPI runs those in a threadpool); handlers that must stay
`async` should push sync DB calls through `asyncio.to_thread` rather than
blocking the loop.

### LLM Traffic Routes Through Bifrost

All LLM calls — including Anthropic — go through the **Bifrost** gateway
(`BIFROST_URL`, default `http://bifrost:8080`), which layers on caching,
centralized cost tracking, and budget enforcement.

- **Never instantiate `Anthropic()` directly.** Import
  `create_anthropic_client` / `create_async_anthropic_client` from
  `services/llm_clients.py` — the single source of truth for client
  construction. The one exception is key-validation endpoints that must hit the
  real upstream to verify a user-supplied credential.
- `services/llm_router.py` dispatches and translates Bifrost's budget/rate-limit
  responses (HTTP 402/429) into `services.budget_service.BudgetExceeded`.
- `services/model_registry.py` resolves component→provider+model assignments and
  owns the pricing/capability catalog.
- Provider API keys are **not** in `.env` — they live in the encrypted secrets
  store and are configured via the UI.

### Service Layer

Business logic lives in `services/`, not in API route handlers. Route handlers in `backend/api/` should delegate to service classes. When adding a feature:
1. Add logic to an existing service or create `services/your_feature_service.py`
2. Add the route in `backend/api/your_feature.py`
3. Register the router in `backend/main.py`

### MCP Tool Access

Agents access external tools through the MCP protocol. Tool definitions live in `mcp-config.json`. New integrations go in `tools/` as MCP server implementations. The `services/mcp_service.py` coordinates tool access.

### Database

- PostgreSQL 16 via SQLAlchemy ORM — models in **`database/models.py`**, sessions
  and the `get_db` dependency in `database/connection.py`
- Schema initialized by `database/init/` SQL files. **Execution order
  differs by deploy path:** docker-compose mounts the directory at
  `/docker-entrypoint-initdb.d`, where Postgres runs files in
  **lexicographic filename order** (the `01_`/`04_`/…/`16_` prefixes
  are authoritative there). The Helm chart, by contrast, iterates
  `helm/vigil/values.yaml`'s `dbInit.sqlFiles` list in the **order
  written there** — prefixes are decorative for the chart path
- pgvector extension for embeddings
- Use `services/database_data_service.py` for data access — do not query the DB directly from API handlers

**When adding or modifying an init SQL file under `database/init/`:** the
chart bundles a *copy* under `helm/vigil/files/database-init/` (Helm can
only read files inside the chart directory). You must (1) copy the file
into the chart bundle, (2) add it to `helm/vigil/values.yaml`
`dbInit.sqlFiles` in the correct execution order, and (3) verify with
`helm template ... | grep -E '^[[:space:]]*apply "NEWFILE\.sql"'` that
the dbInit Job script applies it (a bare `grep NEWFILE.sql` false-matches
the ConfigMap key and the SQL header comment). CI catches step 1
(`Helm Chart / Lint and Template` runs `diff -r` between the two
directories). Skipping step 2 is silent (the file is in the ConfigMap
but never applied); skipping step 1 while keeping the file in
`dbInit.sqlFiles` makes the Job hard-fail at runtime — *unless* the
filename already has a row in `_vigil_schema_versions`, in which case
it SKIPs as already-applied (the marker-table check runs before the
file-existence check, so legacy `003_*` ghost rows on v0.1.x upgrades
don't break `helm upgrade --reuse-values`). See
[`database/init/README.md`](database/init/README.md), which also lists
two filenames that are reserved and must never be reused.

### Authentication

- `DEV_MODE=true` bypasses all auth — use for local development
- **Unset means `false`.** The default lives in one place since #520 —
  `dev_mode: bool = False` in `core/config.py` — so anywhere without a `.env`
  (CI, containers, production) enforces auth. `env.example` ships
  `DEV_MODE=true` and `setup_dev.sh` copies it, which is why a local checkout
  behaves as though `true` were the default. Tests that lean on a developer's
  `.env` — e.g. for `JWT_SECRET_KEY`, which is required once `DEV_MODE` is off —
  will fail in CI; set what you need explicitly
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
- **UI**: Tailwind utility classes + the CSS custom properties in
  `frontend/src/redesign/styles.css`. Reuse the primitives in
  `redesign/shared/` (`ui.tsx`, `formKit.tsx`, `icons.tsx`) — there is no
  component library, so do not add one
- **State/data**: plain hooks (`useState`/`useEffect`) over the axios services;
  React Context for auth/theme/toasts
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
from sqlalchemy.orm import Session
from database.connection import get_db

router = APIRouter(prefix="/api/your-feature", tags=["your-feature"])

@router.get("/")
async def list_items(db: Session = Depends(get_db)):
    return your_feature_service.list(db)   # sync Session — do not await it
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
| `release-please.yml` | Push to `main`, manual | Read Conventional Commits since last tag → open/update a release PR with bumped `VERSION` / `Chart.yaml` (`appVersion` + `version`, lockstep) / `frontend/package.json` / `frontend/package-lock.json` + `CHANGELOG.md`. On merge, push `vX.Y.Z` tag and create the GitHub Release. See `RELEASING.md`. |
| `release.yml` | Version tags (`v*.*.*`) | Build & push `vigil-backend` + `vigil-daemon` images to GHCR → Trivy scan → smoke-test that they start → annotate the GitHub Release with image digests. **Publishes images only — it does not deploy.** Does **not** create the GitHub Release object either (release-please owns that). |
| `helm-chart.yml` | Push/PR touching `helm/` | Verify `database/init/` ↔ chart-bundle copies are in sync (`diff -r`) → `helm lint`/`template` across default, dev, and Bitnami-subchart values → kubeconform → `ct lint` |
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
| `database/init/` | Schema SQL — see Database section for the add/modify checklist |
| `mcp-config.json` | All MCP server definitions |
| `env.example` | Every supported environment variable |
| `docker/docker-compose.yml` | Full local stack definition |
| `docs/AGENTS.md` | Agent reference |
| `docs/INTEGRATIONS.md` | Integration/MCP reference |
| `DEV_MODE.md` | Development auth bypass details |

---

## Submodules

This repo uses three Git submodules:

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
| `mempalace` | `./mempalace` | Agent memory / knowledge palace |

All three are installed as editable packages (`-e ./deeptempo-core`, `-e ./mcp-servers`, `-e ./mempalace`) in `requirements.txt`. If submodules aren't initialized, `start.sh` skips their installation gracefully.

`mempalace` ships its own `tests/benchmarks/`, which a bare `pytest` from the
repo root tries to collect and fails on. Scope your runs the way CI does
(`pytest tests/unit/`, `pytest tests/integration/`).

---

## Security Notes

- Never commit secrets or API keys — use `.env` (gitignored)
- `DEV_MODE=true` disables all auth — **never enable in production**
- Default PostgreSQL password in `docker-compose.yml` must be changed for production
- Bandit runs in CI to catch common Python security issues
- MCP tool calls that perform actions (host isolation, firewall rules) require approval workflow by default — see `services/approval_service.py`
