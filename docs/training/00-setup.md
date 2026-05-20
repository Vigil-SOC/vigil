# Lab 00 — Stand Up Vigil Locally

**Time:** 30 minutes
**Goal:** Get a Vigil instance running on your laptop with populated demo data, so the remaining labs have something to operate on.

---

## Prerequisites

Confirm the checklist in [the lab index](README.md#prerequisites). In particular, **Docker Desktop must be running** and **`ANTHROPIC_API_KEY` must be exported** (or set in `.env`).

---

## Steps

### 1. Clone the repo with submodules

```bash
git clone --recurse-submodules https://github.com/Vigil-SOC/vigil.git
cd vigil
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init --recursive
```

### 2. First-time environment setup

```bash
./setup_dev.sh
```

This creates a Python venv, installs all dependencies, sets up `.env` from `env.example`, and writes `DEV_MODE=true` so you don't have to log in. Takes 3–5 minutes on a fresh machine.

### 3. Enable demo mode

The fastest way to see Vigil with data is to enable demo mode **before** starting the server. Edit `.env`:

```bash
echo "DEMO_MODE=true" >> .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env   # if not already exported
```

Demo mode swaps the database backend for an in-memory synthetic data generator ([services/demo_data_service.py](../../services/demo_data_service.py)), so you see populated findings, cases, and metrics from second one. It is wired in at [services/database_data_service.py:46](../../services/database_data_service.py#L46).

### 4. Start the stack

```bash
./start_web.sh
```

This brings up:

- **PostgreSQL + Redis** via Docker Compose (still needed even in demo mode for the daemon and queue infra).
- **Backend API** on `http://localhost:6987`.
- **Frontend** on `http://localhost:6988`.

Wait ~60 seconds for everything to come up. Watch the logs scroll; you're looking for:

```
INFO:     Uvicorn running on http://0.0.0.0:6987
VITE v5.x.x ready in xxxx ms
```

### 5. Verify

In a second terminal:

```bash
# Backend health
curl -s http://localhost:6987/health | jq

# Demo mode is on
curl -s http://localhost:6987/api/config/demo-mode | jq
# expect: { "enabled": true, ... }

# Findings list has data
curl -s http://localhost:6987/api/findings/ | jq '. | length'
# expect: a positive integer, not 0 and not an error
```

Then open `http://localhost:6988` in your browser. Because `DEV_MODE=true`, you should land directly in the app without a login prompt. See [DEV_MODE.md](../../DEV_MODE.md) for the details on how that bypass works.

---

## Success criteria

You're done with Lab 00 when:

- [ ] `curl http://localhost:6987/health` returns a green status.
- [ ] `curl http://localhost:6987/api/config/demo-mode` returns `"enabled": true`.
- [ ] Opening `http://localhost:6988` shows a populated **Dashboard** — non-zero finding count, recent activity, metrics tiles with numbers.
- [ ] Navigating to **Findings** shows a list of synthetic findings, not an empty table.

If any of these fail, fix before proceeding — every other lab depends on this state.

---

## Common gotchas

- **Empty dashboard.** You forgot to set `DEMO_MODE=true` *before* starting the backend. Stop the server (Ctrl-C in the `./start_web.sh` terminal), add it to `.env`, restart.
- **Backend won't start, port 6987 in use.** A previous run is still alive: `lsof -ti:6987 | xargs kill -9` then retry.
- **Docker errors.** Make sure Docker Desktop is actually running (not just installed). Then `cd docker && docker compose up -d postgres redis`.
- **Submodule errors during `setup_dev.sh`.** The script handles missing submodules gracefully, but features that depend on `deeptempo-core` and `mcp-servers` will be degraded. Run `git submodule update --init --recursive` and re-run `./setup_dev.sh`.
- **`ANTHROPIC_API_KEY` not picked up.** It must be in `.env` *or* exported in the same shell that runs `./start_web.sh`. Confirm with `curl http://localhost:6987/api/claude/sdk-status`.

---

## Findings to capture

Jot down:

1. Total wall-clock time from `git clone` to a populated dashboard. (This is the number your team's onboarding will live or die by.)
2. Anything in the setup script output that looked alarming, even if everything came up.
3. Anything you expected to see on the dashboard that wasn't there.

---

## What you've just stood up

A local Vigil with:

- **Synthetic findings, cases, and metrics** from the demo data service.
- **A live Claude connection** via your API key, ready to invoke agents.
- **The full REST API** at `http://localhost:6987/docs` (FastAPI auto-generated Swagger). Open it now and skim — you'll use it in later labs.
- **Auth disabled** (DEV_MODE), so you can hit the API with `curl` without tokens.

→ Continue to **[Lab 01 — Analyst Workspace Tour](01-analyst-workspace-tour.md)**.
