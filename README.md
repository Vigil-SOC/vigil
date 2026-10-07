# Vigil SOC

Vigil is an open source, agentic AI SOC released under Apache 2.0. Your playbooks are plain-text Markdown files, your agent logic is readable code, and your integrations use an open standard ([MCP](https://modelcontextprotocol.io/)). It is a capability you own, not a black box you rent.

Vigil runs on its own: a local clone, Docker, and any supported LLM provider (a local Ollama model works). [LogLM](https://www.deeptempo.ai/platform), DeepTempo's cybersecurity foundation model for [behavioral anomaly detection](https://www.deeptempo.ai/learning-center/behavioral-anomaly-detection), is an optional MCP integration you enable in Settings, not a prerequisite. Docs and community: [vigilsoc.org](https://vigilsoc.org).

**Vigil 1.0 is coming.** We believe it will be the first AI SOC built for a human *on* the loop rather than *in* it. Join the upcoming office hours for a preview.

The project draws on the founders' experience building [StackStorm](https://github.com/StackStorm/st2) and supporting teams, including Netflix, that used it to reach high levels of automation carefully. One lesson carries through: the system can only demote its own autonomy, and only humans can promote it. Vigil checks projected cost and confidence before acting, and asks a human when either looks off.

Vigil rests on three parts — **agents** you can read, fork, and rewire; **workflows** defined as Markdown files you edit directly; and **integrations** connected over MCP — plus the most important one: **you**. Contribute feedback, code, a star, or memes on Discord.

---

## Agents

Vigil ships a set of built-in specialist agents covering the core SOC roles — triage, investigation, threat hunting, correlation, response, reporting, ATT&CK mapping, forensics, threat intel, compliance, malware and network analysis — and you can add your own from the console. The set grows and changes with the project; the built-in definitions live in [`core/agents/builtins.py`](core/agents/builtins.py) and are described at [vigilsoc.org/docs/agents](https://vigilsoc.org/docs/agents/).

Every agent can call Vigil's backend tools (findings, cases, approvals, detections, ATT&CK, threat intel, memory, skills) and the tools of whichever MCP integrations you have connected. Each agent's tool list, prompt, model, and extended-thinking budget are visible in its definition.

Response actions are proposed as approval requests. A request from the Response agent always waits for an analyst. The daemon's auto-responder routes by confidence: by default, at or above 0.90 is auto-approved, 0.85–0.90 gets a quick review, 0.70–0.85 needs analyst review, and below 0.70 escalates (configurable). The approval pipeline records `isolate_host` actions but does not execute them; host isolation or quarantine happens only when an agent calls an EDR integration's tool directly.

## Workflows

A workflow is a `WORKFLOW.md` file: YAML frontmatter that says what runs, and a Markdown body that explains it. Built-in workflows live under [`core/workflows/definitions/`](core/workflows/definitions/); edit them there, build your own from the Workflows screen, or disable the ones you don't use.

A workflow's `run_kind` decides how it executes:

| `run_kind` | How it runs | Built-in examples |
|---|---|---|
| `compose` (default) | Walks the declared `phases` in order, each phase handled by the named agent with the listed tools | Your custom workflows |
| `investigate` | A single lead agent works the objectives and narrative; no phases | Incident Response, Full Investigation, Forensic Analysis, Cloud Incident |
| `root_cause` | A single agent traces a confirmed compromise backward, step by step | Root Cause Analysis |
| `hunt` / `adjudicate` | A lead runs a hypothesis loop, dispatching worker agents and deciding each next move from the evidence | Threat Hunt, Shadow Adjudication |

Start one from chat, for example `"Run incident response on finding f-20260215-abc123"`, or from the Workflows screen.

### Create your own workflow

```markdown
---
name: phishing-triage
description: "Triage and investigate phishing reports from user submissions."
use_case: "A user reports a suspicious email and the SOC needs to assess, investigate, and contain."
trigger_examples:
  - "Run phishing triage on finding f-20260401-abc123"
objectives:
  - "Decide whether the reported mail is malicious"
  - "Contain it without waiting on a second report"
phases:
  - id: assess
    agent: triage
    name: "Assess the Report"
    tools: [get_finding, list_findings]
    instructions: |
      Fetch the finding, extract sender/domain/URLs, score severity, check for
      known-bad indicators. Hand on the verdict and the indicators you found.

  - id: investigate
    agent: investigator
    name: "Investigate"
    tools: [get_finding, search_detections]
    instructions: |
      Correlate with detection rules. Build an evidence timeline. Hand on the
      timeline and related findings.

  - id: contain
    agent: responder
    name: "Contain"
    tools: [get_case, update_case]
    approval_required: true
    instructions: |
      If confirmed malicious: block the sender domain, quarantine matching emails,
      and plan remediation with confidence scores.
---

# Phishing Triage Workflow

An overview for whoever reads this file. The `phases` above are what actually
runs, in the order written.
```

`approval_required: true` holds a phase for a human before it acts. Scaffold a new file with:

```bash
python scripts/create_workflow.py phishing-triage --agents triage,investigator,responder
# creates core/workflows/definitions/phishing-triage/WORKFLOW.md
```

---

## Integrations

Vigil connects agents to your existing tools through [MCP](https://modelcontextprotocol.io/). Integrations span SIEM and log search, EDR/XDR, cloud security (AWS, Azure, GCP), identity, threat intel, sandboxes, detection engineering, ticketing, chat and paging, network security, and data pipelines. The catalogue changes as vendors ship official MCP servers and contributors add new ones; [`mcp-config.json`](mcp-config.json) is the current list, and [vigilsoc.org/docs/integrations](https://vigilsoc.org/docs/integrations/) describes each one.

An integration is one of two things:

- **An upstream MCP server**: a vendor's or community's own server, pinned to a version in `mcp-config.json` (npx, uvx, Docker, or a remote endpoint).
- **A vendor slice**: a server Vigil maintains at `core/integrations/<vendor>/tool.py`, used where no suitable upstream server exists. See [vendor slices](https://vigilsoc.org/docs/vendor-slices/).

What an integration can do depends on its server: some are read-only lookups, others can act (for example, Microsoft Defender isolation or Carbon Black quarantine). The integrations docs list each one's tools.

Enable and configure integrations under **Settings → Integrations**, or generate one from API docs with the **Custom Integration Builder**. If you build an integration you find useful, someone else will too. Please contribute it.

**Vigil's own MCP server.** Vigil serves its SOC operations (findings, cases, approvals, hunts) at `/mcp`, the same tools its agents use. The finding, case, and approval tools that mirror frozen `/api/v1` operations are frozen: their names and input schemas are pinned in [`tools/mcp/frozen_tools.snapshot.json`](tools/mcp/frozen_tools.snapshot.json). The rest are served under the `0.x` terms in [`SECURITY.md`](SECURITY.md#supported-versions).

**Detection rules.** The detection-engineering integration ([security-detections-mcp](https://www.npmjs.com/package/security-detections-mcp)) indexes community rule sets (Sigma, Splunk ESCU, Elastic, KQL) for search, coverage analysis, and gap identification. Vigil does not ship the rules; fetch them with `./scripts/setup_detection_repos.sh` (or `SETUP_DETECTION_REPOS=1 ./setup_dev.sh`).

---

## Quick Start

```bash
git clone https://github.com/Vigil-SOC/vigil.git
cd vigil
cp env.example .env   # set AGENT_INTERNAL_TOKEN (see below)
./start.sh
```

Then open http://localhost:6988 and create the admin account on the bootstrap screen.

`start.sh` provisions the pinned Python with [uv](https://docs.astral.sh/uv/), installs dependencies, starts PostgreSQL, Redis, and the Bifrost LLM gateway in Docker, starts a host Ollama if one is installed (optional), initializes the schema and reference data, and launches the API, the agent layer, and the frontend. No LogLM or cloud API key is needed to reach a running UI.

- **`AGENT_INTERNAL_TOKEN`** lets the agent layer talk to the API. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Without it the stack still starts, but workflow runs stay queued.
- **Authentication is on by default.** No account or default password ships; the first visit creates the admin. `start.sh` creates the JWT signing secret at `~/.vigil/jwt_secret`. For an unauthenticated instance on your own machine, set `DEV_MODE=true` in `.env`; the backend announces the bypass on every startup.

> **Stable vs. development build:** `main` is the active development branch. For a tested build, check out a release tag (`git checkout v<version>`) or pull a published image (`docker pull ghcr.io/vigil-soc/vigil-backend:<version>`). Releases are listed on the [releases page](https://github.com/Vigil-SOC/vigil/releases/latest).
>
> Released images are signed keyless by [`.github/workflows/release.yml`](.github/workflows/release.yml). To verify (the image tag drops the leading `v`; the certificate identity uses the git tag):
>
> ```bash
> cosign verify \
>   --certificate-identity https://github.com/Vigil-SOC/vigil/.github/workflows/release.yml@refs/tags/v<version> \
>   --certificate-oidc-issuer https://token.actions.githubusercontent.com \
>   ghcr.io/vigil-soc/vigil-backend:<version>
> ```

### Prerequisites

- **Docker** (running): PostgreSQL, Redis, and Bifrost run in containers.
- **Node.js 20+**: runs the agent layer and the frontend. Without Node, neither starts and workflows cannot run.
- **Git**.
- **No system Python needed**: `start.sh` provisions the version pinned in `.python-version` with uv, independent of any Python you already have.
- **An LLM provider** (optional to reach the UI): Anthropic (default), OpenAI, Google Vertex, or Ollama (local, no key). Configure it under **Settings → AI models → Providers & Keys**; see the [Bifrost notes](https://vigilsoc.org/docs/bifrost/). With no provider key, `scripts/local_model.sh` serves a small local model Bifrost can reach and prints the model id to use.

### Run

```bash
./start.sh              # interactive; Ctrl+C to stop
./start.sh -d           # background: logs/ + pidfiles, also starts the SOC daemon
./start.sh --with splunk   # add a profiled service (splunk, kafka, pgadmin, jaeger,
./start.sh --all           #   prometheus, grafana, otel-collector), or all of them
```

Core services come from `.vigil-autostart` (or `$AUTOSTART_SERVICES`), defaulting to `postgres redis bifrost ollama`.

| | URL |
|---|---|
| Frontend | http://localhost:6988 |
| API | http://localhost:6987 |
| API docs | http://localhost:6987/docs |

### Shutdown

```bash
./shutdown_all.sh            # stop native processes; containers keep running
./shutdown_all.sh -d         # stop everything; data is kept
./shutdown_all.sh -d --full  # stop everything and PERMANENTLY DELETE all data volumes
```

`-d --full` runs `docker compose down -v`, which deletes the database, Bifrost keys, `master.key`, investigation files, and the default on-box backup repository. Keep backups outside the compose volumes (`VIGIL_BACKUP_REPO`) first. See `./shutdown_all.sh --help`.

<details>
<summary>Manual install (separate terminals)</summary>

```bash
# Python environment (uv fetches the interpreter in .python-version)
uv python install
uv venv --python "$(cat .python-version)" --python-preference only-managed venv
source venv/bin/activate
uv pip install -r requirements.lock
cp env.example .env   # set AGENT_INTERNAL_TOKEN
export PYTHONPATH="${PWD}:${PYTHONPATH}"

# 1. Containers
docker compose -f infra/docker/docker-compose.yml up -d postgres redis bifrost

# 2. Schema and reference data (no admin is seeded)
python scripts/init_schema.py
python scripts/seed_reference_data.py

# 3. API
uvicorn services.api.main:app --host 127.0.0.1 --port 6987 --reload

# 4. Agent layer (drains the queue workflow runs are enqueued to)
scripts/agent_up.sh
# optional: python -m services.worker   (ARQ LLM worker)

# 5. Frontend
cd clients/web && npm install && npm run dev
```

</details>

### Run with Docker (full stack)

```bash
docker compose --env-file .env -f infra/docker/docker-compose.yml up -d
```

Pass the repo-root `.env` explicitly, or `AGENT_INTERNAL_TOKEN` reaches the containers empty. The `backend` container refuses to start without `JWT_SECRET_KEY`. Set it in `.env`, for example `export JWT_SECRET_KEY="$(cat ~/.vigil/jwt_secret)"` if `start.sh` has run before, or `openssl rand -base64 48`.

The default set runs the database, cache, gateway, backup, API, and agent layer: everything a chat-driven workflow needs. Opt-in profiles add more: `daemon` (the SOC daemon plus the ARQ worker, for headless 24/7 monitoring), `dev`, `observability`, `splunk`, `kafka`, `elastic`, and `misp`:

```bash
docker compose --env-file .env -f infra/docker/docker-compose.yml --profile daemon up -d
```

On the host, `./start.sh -d` also runs the daemon (`services/daemon/main.py`), or run it alone against a running stack with `python services/daemon/main.py`.

### Install on Kubernetes

```bash
helm install vigil ./infra/helm/vigil \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set secrets.postgresPassword="$(openssl rand -hex 24)" \
  --set secrets.jwtSecretKey="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
```

See the [Helm guide](https://vigilsoc.org/docs/helm/) for values, external Postgres/Redis, ingress, and troubleshooting.

### Desktop app

The desktop app packages the stack without a source tree. It runs the backend and agent images from an offline tarball, alongside a bundled Bifrost gateway, so **Docker must be installed and running**. Bifrost reaches a host Ollama through `host.docker.internal`.

```bash
bash clients/desktop/scripts/bundle-image.sh linux/arm64   # tarball is arch-specific
cd clients/desktop && npm run dist                          # macOS DMG / Linux AppImage
```

Local builds are ad-hoc signed, not notarized, so macOS may report the app as "damaged". Clear the quarantine with `xattr -dr com.apple.quarantine /Applications/Vigil.app`, or use **System Settings → Privacy & Security → Open Anyway**.

### Tests

```bash
source venv/bin/activate
pytest tests/unit tests/security -m "not external_service"   # same invocation as CI; no LLM key needed
```

---

## Getting help: the support bundle

`vigil-support.sh` writes one redacted `tar.gz` you can attach to a support request. It is POSIX `sh`, runs on Linux and macOS, and uploads nothing.

```bash
sh scripts/vigil-support/vigil-support.sh   # from a checkout
```

Without a checkout, download `vigil-support-<version>.tar.gz` and its `.sha256` from the [release](https://github.com/Vigil-SOC/vigil/releases/latest), verify it, extract it, and run `sh vigil-support/vigil-support.sh`. Vigil Desktop shows the exact command under **Support Bundle Command…** in the tray menu. For a Helm install, run it from your own machine with your current `kubectl` context: `--mode helm [--release NAME --namespace NS]`.

Re-run with `sudo` to include system logs that need elevation; the bundle still examines, and belongs to, the invoking user's install. `sudo` resets `VIGIL_DIR`, so pass `--state-dir` for a non-default State Directory.

> **DATA NOTICE:** the bundle holds host information: hostname, the full process list with command lines, system logs, and disk usage. Known credential formats are redacted, but secrets in free log text cannot be guaranteed caught. Review it before you share it.

When it finds a Vigil install, the bundle also collects its configuration, health output, container status, and logs. Secrets, keys, and database contents are never included; `SUMMARY.txt` lists what was left out. See [`scripts/vigil-support/README.md`](scripts/vigil-support/README.md).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Web console (React)  ·  Chat  ·  Desktop app  ·  /mcp clients    │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  API (Python / FastAPI)                                           │
│  Findings · Cases · Approvals · Detections · ATT&CK · Workflows   │
└──────────────────────────────────────────────────────────────────┘
                │ enqueue runs (BullMQ / Redis)      ▲ backend tools
                ▼                                    │
┌──────────────────────────────────────────────────────────────────┐
│  Agent layer (TypeScript)                                         │
│  Workflow runtimes: compose · investigate · root cause · hunt     │
│  Agents call backend tools and MCP integration tools              │
└──────────────────────────────────────────────────────────────────┘
        │ model calls                       │ tool calls
        ▼                                   ▼
┌──────────────────────────┐  ┌────────────────────────────────────┐
│  Bifrost LLM gateway     │  │  MCP integrations                  │
│  Anthropic · OpenAI ·    │  │  SIEM · EDR · cloud · identity ·   │
│  Vertex · Ollama         │  │  intel · sandbox · ticketing · …   │
└──────────────────────────┘  └────────────────────────────────────┘

  PostgreSQL: findings, cases, approvals, runs, settings
  SOC daemon (optional): alert polling and auto-enrichment
```

## Project structure

```
vigil/
├── core/              # Shared Python library: domains (findings, cases, llm,
│   │                  #   integrations, …) over a storage/platform tier
│   ├── agents/        # Built-in agent definitions
│   ├── integrations/  # Vendor slices and the MCP registry
│   └── workflows/definitions/   # Built-in WORKFLOW.md files
├── services/
│   ├── api/           # FastAPI backend
│   ├── agent/         # TypeScript agent layer (workflow runtimes, worker + serve)
│   ├── daemon/        # Headless autonomous SOC
│   └── worker/        # ARQ LLM worker
├── clients/
│   ├── web/           # React + Tailwind frontend
│   └── desktop/       # Desktop app
├── tools/mcp/         # Vigil's own MCP server and frozen tool snapshot
├── mcp-config.json    # MCP integration catalogue
└── infra/             # Docker Compose, Helm chart, DB init
```

## Documentation

Guides live at **[vigilsoc.org/docs](https://vigilsoc.org/docs/)**:

| Doc | Contents |
|-----|----------|
| [Agents](https://vigilsoc.org/docs/agents/) | Built-in agents and how to customize them |
| [Integrations](https://vigilsoc.org/docs/integrations/) | MCP integrations and setup |
| [Detection engineering](https://vigilsoc.org/docs/detection-engineering/) | Rule search, coverage, and gaps |
| [Chat-driven case management](https://vigilsoc.org/docs/chat-case-management/) | Building cases in natural language |
| [Configuration](https://vigilsoc.org/docs/configuration/) | Environment variables, secrets, deployment |
| [Helm](https://vigilsoc.org/docs/helm/) | Chart values, secrets, install |
| [Splunk testing](https://vigilsoc.org/docs/splunk-testing/) · [Postgres → Splunk](https://vigilsoc.org/docs/postgres-to-splunk/) | Test data and export scripts |
| [Contributing](https://vigilsoc.org/docs/contributing/) | How to contribute, DCO |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting, supported versions, disclosure |
| [VERSIONING.md](VERSIONING.md) | What is frozen, what is not, and how the contract changes |

## Contributing

Contributions are welcome: bug fixes, integrations, agent prompts, workflows, or new agents. Join the community on [Discord](https://discord.gg/Kw68sPJU).

1. Fork the repo and create a feature branch.
2. Make your changes and test them.
3. Open a pull request with a clear description.

See the [contributing guide](https://vigilsoc.org/docs/contributing/) for the full process.

## License

Apache 2.0. See [LICENSE](LICENSE).

## References

- [Vigil](https://vigilsoc.org/): project homepage
- [DeepTempo](https://deeptempo.ai): Vigil sponsor; LogLM connects via MCP as an optional detection layer
- [Model Context Protocol](https://modelcontextprotocol.io/): MCP specification
- [SOCBench](https://socbench.org): open benchmark for AI in cybersecurity operations
