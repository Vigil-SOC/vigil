# Vigil

Vigil is the leading open source AI SOC: an agentic SOC with 13 specialized AI agents, 30+ MCP integrations, and 7,200+ community detection rules, released under Apache 2.0. Your playbooks are plain-text files, your agent logic is readable Python, and your integrations use an open standard ([MCP](https://modelcontextprotocol.io/)). Every proprietary AI SOC on the market is a black box you rent. Vigil is a capability you own.

Vigil runs on its own: a local clone, Docker, and any LLM provider (a local Ollama model works). [LogLM](https://www.deeptempo.ai/platform), DeepTempo's cybersecurity foundation model for [behavioral anomaly detection](https://www.deeptempo.ai/learning-center/behavioral-anomaly-detection), is an optional MCP integration you enable in Settings, not a prerequisite. Measured in the open by [SOCBench](https://socbench.org). Docs and community: [vigilsoc.org](https://vigilsoc.org).

The inspiration for the project is in part StackStorm and the experience of some of the founders of this project had in building the Linux Foundation project [StackStorm](https://github.com/StackStorm/st2) and in supporting Netflix and others who used StackStorm to achieve, carefully, very high levels of automation.  You'll sometimes hear us talking about the journey towards full autonomy and lessons learned.  One lesson - the system can only demote itself and only humans can promote additional autonomy.  You'll find this playing out on the way Vigil is designed; for example Vigil will check thresholds for projected costs and confidence levels in completion before executing an automation.  If it looks dodgy or too expensive, it'll double check with the humans before moving ahead.  

The project is built on three pillars: **Agents** — 13 specialized AI agents you can read, fork, and rewire; **Workflows** — multi-agent playbooks defined as Markdown files you edit directly; and **Integrations** — 30+ tool connections via MCP that you configure, not a vendor. The most important pillar is **YOU** — this is your project. Contribute via feedback, code, a repo star, memes on Discord, or otherwise.

---

## 12 Specialized AI Agents

Every agent has access to 19 backend tools via Agent SDK and 100+ additional tools via MCP. Agents are the building blocks that Workflows orchestrate.

| Agent | Role | Thinking | Key Capability |
|-------|------|----------|----------------|
| **Triage** | Rapid alert assessment | Fast | Severity scoring, false-positive filtering, escalation decisions |
| **Investigator** | Root cause analysis | Deep | Evidence collection, timeline reconstruction, cross-source correlation |
| **Threat Hunter** | Proactive hunting | Deep | Hypothesis-driven anomaly detection, pattern intelligence from 7,200+ rules |
| **Correlator** | Multi-signal linking | Deep | Campaign identification, attack chain reconstruction, entity mapping |
| **Responder** | Containment actions | Fast | NIST IR containment, blast radius assessment, confidence-scored approval requests |
| **Reporter** | Documentation | Balanced | Executive summaries, technical reports, audience-tailored content |
| **MITRE Analyst** | ATT&CK mapping | Deep | Technique identification, coverage analysis, gap prioritization, detection templates |
| **Forensics** | Digital forensics | Deep | Artifact analysis, chain of custody, multi-domain examination |
| **Threat Intel** | IOC enrichment | Deep | Actor attribution, campaign tracking, OSINT integration |
| **Compliance** | Regulatory checks | Balanced | NIST, ISO, PCI-DSS, HIPAA, GDPR, SOC 2 assessment |
| **Malware Analyst** | Malware examination | Deep | Static/dynamic analysis, family classification, C2 identification |
| **Network Analyst** | Traffic analysis | Deep | Flow analysis, protocol anomalies, lateral movement detection |

## Workflows — One-Click Multi-Agent Workflows

Workflows are the operational core of Vigil. Each workflow chains multiple specialized AI agents into an end-to-end playbook that executes with a single command. No manual hand-offs, no copy-pasting between tools — the agents coordinate automatically.  

| Workflow | Agents | What It Does |
|----------|--------|-------------|
| **Incident Response** | Triage → Investigator → Responder → Reporter | NIST IR framework: triage an alert, investigate root cause, contain the threat, produce an audit-ready report |
| **Full Investigation** | Investigator → MITRE Analyst → Correlator → Responder → Reporter | Deep-dive with ATT&CK mapping, cross-signal correlation, response planning, and comprehensive documentation |
| **Threat Hunt** | Threat Hunter → Network Analyst → Malware Analyst → Threat Intel → Reporter | Hypothesis-driven hunting across network, endpoint, and threat intel — with IOC enrichment and detection recommendations |
| **Forensic Analysis** | Forensics → Malware Analyst → Network Analyst → Reporter | Post-incident digital forensics with evidence preservation, chain-of-custody documentation suitable for legal proceedings |

**How it works:** Say `"Run incident response on finding f-20260215-abc123"` and the system sequences four agents — triage scores the alert, investigator digs into root cause, responder submits containment actions with confidence-based approval, and reporter generates the final documentation.

Workflows are defined as `WORKFLOW.md` files under `core/workflows/definitions/` and are fully customizable. Create your own by defining the agent sequence, tools used, and phase-by-phase instructions.

```
core/workflows/definitions/
├── incident-response/WORKFLOW.md
├── full-investigation/WORKFLOW.md
├── threat-hunt/WORKFLOW.md
├── forensic-analysis/WORKFLOW.md
├── root-cause-analysis/WORKFLOW.md
├── cloud-incident/WORKFLOW.md
└── shadow-adjudication/WORKFLOW.md
```

### Create Your Own Workflow in 60 Seconds

Every workflow is a Markdown file. Here's what one looks like inside:

```markdown
---
name: phishing-triage
description: "Triage and investigate phishing reports from user submissions."
use_case: "A user reports a suspicious email and the SOC needs to assess, investigate, and contain."
trigger_examples:
  - "Run phishing triage on finding f-20260401-abc123"
  - "Investigate this phishing report"
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
runs, in the order written — the agents and tools shown on the Workflows screen
are read off them.
```

Edit this file. That's it. No vendor ticket, no professional services, no YAML/JSON schema to learn.

Scaffold a new workflow instantly with the CLI:

```bash
python scripts/create_workflow.py phishing-triage
# creates core/workflows/definitions/phishing-triage/WORKFLOW.md with a commented template
```

---

## Integrations - 

Vigil uses the [Model Context Protocol](https://modelcontextprotocol.io/) to connect agents to your existing tools. These MCP servers give every agent real-time access to your SIEM, EDR, threat intel, sandbox, ticketing, and communication platforms — all through a unified interface.

| Category | Integrations | Tools |
|----------|-------------|-------|
| **SIEM** | Splunk | Natural language → SPL, search by IP/host/user, index listing |
| **EDR / XDR** | CrowdStrike | Alert lookup, host isolation/unisolation, host status |
| **Threat Intel** | VirusTotal, Shodan, AlienVault OTX, MISP | Hash/IP/domain/URL reputation, host recon, pulse matching, IOC search |
| **Sandbox** | Hybrid Analysis, Joe Sandbox, ANY.RUN | File submission, report retrieval, IOC extraction |
| **Timeline** | Timesketch | Forensic timeline analysis, evidence export |
| **Detection Engineering** | Security-Detections-MCP | 7,200+ rules (Sigma, Splunk, Elastic, KQL), 71 tools, coverage analysis, gap identification |
| **Ticketing** | Jira | Issue creation, updates, search |
| **Communication** | Slack | Alerts, channel creation, file uploads |
| **Data Pipeline** | Cribl Stream | Log normalization, noise filtering, multi-destination routing |
| **Core** | Vigil | Built-in SOC operations: findings, cases, approvals, hunts — the same tools an external caller reaches at `/mcp` |

**Coming soon:** AWS Security Hub, Azure Sentinel, GCP Security, Okta, Microsoft Defender, SentinelOne, Carbon Black, PagerDuty.

MCP servers live in each vendor's slice as `core/integrations/<vendor>/tool.py` and are configured via the Settings UI or `mcp_config.json`. Add a new integration by adding a slice with an MCP server in it — see [vendor slices](https://vigilsoc.org/docs/vendor-slices/) — or use the built-in Custom Integration Builder to generate one from API docs.  If you build an integration that you find useful, chances are someone else will as well.  Please contribute!

---

## Quick Start

```bash
git clone https://github.com/Vigil-SOC/vigil.git
cd vigil
./start.sh
```

> **Note:** Docker must be running before you start. The startup script handles everything else: provisions the Python virtual environment, installs dependencies, starts PostgreSQL, Redis, and the Bifrost LLM gateway in Docker, starts a host Ollama if one is installed (optional — the script continues without it), initializes the database schema and reference data, installs frontend packages, and launches the backend, frontend, and agent layer. No LogLM or cloud API key is needed to reach a running UI.

To run workflows, set `AGENT_INTERNAL_TOKEN` before the first start: `cp env.example .env`, fill in the token (generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`), then `./start.sh`. Without it the stack still comes up, but `./start.sh` warns that the agent layer did not start and workflow runs stay queued; edit `.env` and rerun.

Authentication is on by default. No admin user is seeded, so the first visit to http://localhost:6988 shows a bootstrap screen where you create the admin account; `start.sh` mints the JWT signing secret it needs at `~/.vigil/jwt_secret`. For an unauthenticated instance on your own machine, set `DEV_MODE=true` in `.env` — the bypass is documented there, and the backend announces it on every startup.

> **Stable build vs. development build:** The Quick Start above clones `main` —
> the active development branch (latest, *unreleased* code). For a stable,
> tested build, use a released version instead: pull a published image
> (`docker pull ghcr.io/vigil-soc/vigil-backend:<version>`) or check out a
> release tag before running (`git checkout v<version>`). Find the newest
> version on the [releases page](https://github.com/Vigil-SOC/vigil/releases/latest).
>
> Released images are signed keyless by
> [`.github/workflows/release.yml`](.github/workflows/release.yml). Confirm a
> pull came from that workflow (image tag drops the leading `v`; the certificate
> identity uses the git tag):
>
> ```bash
> cosign verify \
>   --certificate-identity https://github.com/Vigil-SOC/vigil/.github/workflows/release.yml@refs/tags/v<version> \
>   --certificate-oidc-issuer https://token.actions.githubusercontent.com \
>   ghcr.io/vigil-soc/vigil-backend:<version>
> ```

### Prerequisites

- **No Python needed** — `./start.sh` provisions the pinned interpreter
  (`.python-version`, currently 3.12) with [uv](https://docs.astral.sh/uv/),
  independent of any system, conda, or pyenv Python you already have
- **Node.js 18+** (for frontend)
- **Docker Desktop** (must be running — used for PostgreSQL, Redis, and Bifrost)
- **Git**
- An LLM provider. Vigil supports Anthropic Claude (default), OpenAI, and Ollama (local, no key) — configure providers in Settings → AI Config. See the [Bifrost gateway](https://vigilsoc.org/docs/bifrost/) notes for the multi-provider setup. *(optional for initial testing)*. With the compose stack up and no provider key, `scripts/local_model.sh` serves a small local model where Bifrost can reach it and prints the id to address it by.

### First Login

The user table starts empty and the first visit to http://localhost:6988
opens a bootstrap screen (backed by `/api/auth/bootstrap`) where you create
the admin account. No default credentials ship with the repo, and nothing in
it creates an account with a password you did not choose. With `DEV_MODE=true`
in `.env` there is no login at all.

### Manual Install

<details>
<summary>Click to expand manual setup steps</summary>

```bash
git clone https://github.com/Vigil-SOC/vigil.git
cd vigil

# Environment (authentication on by default; set DEV_MODE=true here to bypass it locally)
cp env.example .env
# LLM provider keys (Anthropic / OpenAI / Ollama) are configured in the
# web UI at Settings → AI / LLM Providers — not in .env.

# Backend setup. uv fetches the interpreter named in .python-version, so this
# does not use (or disturb) any Python already on your PATH. Installing uv:
# https://docs.astral.sh/uv/getting-started/installation/
uv python install
uv venv --python "$(cat .python-version)" --python-preference only-managed venv
source venv/bin/activate
uv pip install -r requirements.lock

# Frontend setup
cd clients/web
npm install
cd ../..
```

</details>

### Install on Kubernetes

A production-style Helm chart lives at [`infra/helm/vigil/`](infra/helm/vigil/):

```bash
helm install vigil ./infra/helm/vigil \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set secrets.postgresPassword="$(openssl rand -hex 24)" \
  --set secrets.jwtSecretKey="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
```

See the [Helm values guide](https://vigilsoc.org/docs/helm/) for the full
values reference, external Postgres/Redis setup, ingress configuration, and
troubleshooting.

### Run

**Option A: All-in-one (recommended)**

```bash
# Interactive mode (keeps terminal attached, Ctrl+C to stop)
./start.sh

# OR background mode (frees terminal; logs/ + pidfiles, also starts the
# SOC daemon on the host — the ARQ worker runs in both modes)
./start.sh -d

# Add a profiled service (splunk, kafka, pgadmin, jaeger, prometheus,
# grafana, otel-collector), or all of them
./start.sh --with splunk
./start.sh --all
```

Core services come from `.vigil-autostart` (or `$AUTOSTART_SERVICES`), defaulting to `postgres redis bifrost ollama`. Ollama is host-native and optional: if it is not installed the script warns and continues.

**Option B: Manual (separate terminals)**

```bash
# Terminal 1: Start the Docker services (Docker must be running)
docker compose -f infra/docker/docker-compose.yml up -d postgres redis bifrost

# Terminal 2: Initialize the schema and reference data (no admin is seeded;
# the first visit to the UI is the bootstrap screen)
[ -f .env ] || cp env.example .env   # then set AGENT_INTERNAL_TOKEN in it
source venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
python scripts/init_schema.py
python scripts/seed_reference_data.py

# Terminal 3: Start backend
source venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
uvicorn services.api.main:app --host 127.0.0.1 --port 6987 --reload

# Terminal 4: Start the agent layer (drains the agent-runs queue that
# workflow runs are enqueued to; needs AGENT_INTERNAL_TOKEN set in .env).
# Optionally also `python -m services.worker` for the ARQ consumers.
scripts/agent_up.sh

# Terminal 5: Start frontend
cd clients/web && npm run dev
```

### Shutdown

```bash
./shutdown_all.sh              # Stop native processes only (Docker keeps running)
./shutdown_all.sh -d           # Stop native processes + Docker containers
./shutdown_all.sh -d --full    # Stop + remove containers and volumes
```

### Access

- **Frontend**: http://localhost:6988
- **API**: http://localhost:6987
- **API Docs**: http://localhost:6987/docs

### Run with Docker (Full Stack)

The compose file is [`infra/docker/docker-compose.yml`](infra/docker/docker-compose.yml). Pass the repo-root `.env` explicitly — compose otherwise looks for one next to the compose file, and `AGENT_INTERNAL_TOKEN` would reach the containers empty. Plain `up` is the investigation set:

```bash
docker compose --env-file .env -f infra/docker/docker-compose.yml up -d
```

Starts `postgres`, `db-seed`, `redis`, `bifrost`, `backend`, `agent-worker`, and `agent-serve` — the services a chat-driven workflow run uses. The SOC daemon is not part of this set.

```bash
# Add federation polling, auto-enrichment, and the ARQ worker
docker compose --env-file .env -f infra/docker/docker-compose.yml --profile daemon up -d
```

`--profile daemon` adds `soc-daemon` and `llm-worker`. Other opt-in profiles (`dev`, `observability`, `splunk`, `kafka`, `elastic`, `misp`) work the same way.

> The `backend` container runs with auth on and fails closed without `JWT_SECRET_KEY`, which the compose file does not forward yet. Until it does, supply one through a second compose file (`-f my-override.yml` with `services.backend.environment: [JWT_SECRET_KEY=<random>]`).

### Run SOC Daemon (Headless Mode)

For autonomous 24/7 monitoring, run the `daemon` compose profile above, or on the host:

```bash
# Background mode launches services/daemon/main.py and the ARQ worker
# alongside the backend; logs land in logs/daemon.log and logs/llm_worker.log
./start.sh -d

# Or run the daemon on its own against a running stack
source venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
python services/daemon/main.py
```

### Tests

The no-service suite needs neither LogLM nor a cloud LLM key — it is the same invocation CI runs:

```bash
source venv/bin/activate
pytest tests/unit tests/security -m "not external_service"
```

"Passes on a local model" is a manual check, not a CI badge. With Postgres, Redis, and Bifrost up, `scripts/local_model.sh` (override the tag with `VIGIL_LOCAL_MODEL` or the first argument) prints a model id; send that as `model` on an authenticated `POST /api/claude/chat/stream`. The run proves wiring, not model quality.

### Desktop App (Standalone)

The desktop app packages the full stack into an installable bundle — no
source tree required. It runs the backend as a Docker container loaded from
an offline image tarball, alongside a bundled Bifrost LLM gateway, so
**Docker Desktop must be installed and running**. Local LLMs (Ollama) work
out of the box: Bifrost is configured to route any pulled/built model and to
reach Ollama on the host via `host.docker.internal`.

Build a local DMG (Apple Silicon shown; the image tarball is arch-specific):

```bash
# 1. Build the backend and agent images from source and stage them as an offline tarball
bash clients/desktop/scripts/bundle-image.sh linux/arm64

# 2. Package the app (copies the Bifrost config, bundles the tarball)
cd clients/desktop && npm run dist
# -> clients/desktop/release/Vigil-<version>-arm64.dmg
```

> **macOS Gatekeeper (unsigned build).** Locally built DMGs are ad-hoc
> signed, not notarized, so macOS quarantines the app and shows *"Vigil is
> damaged and can't be opened"* or *"can't be opened because Apple cannot
> check it"*. Clear the quarantine attribute after installing to
> `/Applications`:
>
> ```bash
> xattr -dr com.apple.quarantine /Applications/Vigil.app
> ```
>
> Alternatively, open it once via **System Settings → Privacy & Security →
> Open Anyway**. Proper signing/notarization is pending.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Workflows Layer                             │
│  Incident Response │ Full Investigation │ Threat Hunt │ Forensics │
│              (Multi-agent workflow orchestration)                  │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                   13 Specialized AI Agents                        │
│  Triage │ Investigator │ Hunter │ Correlator │ Responder │ ...   │
└──────────────────────────────────────────────────────────────────┘
                │                              │
     Agent SDK (23 tools)              MCP (100+ tools)
                │                              │
                ▼                              ▼
┌──────────────────────────┐  ┌────────────────────────────────────┐
│     Backend Services     │  │          MCP Servers (30+)         │
│  Detections (7,200+)     │  │  Splunk │ CrowdStrike │ VirusTotal │
│  Case Management         │  │  Shodan │ Jira │ Slack │ Cribl    │
│  Approvals │ MITRE ATT&CK│  │  Timesketch │ MISP │ ANY.RUN      │
│  Similarity Search       │  │  Hybrid Analysis │ Joe Sandbox    │
└──────────────────────────┘  └────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│                  Data Sources + PostgreSQL                         │
│  Logs │ Alerts │ Findings │ Embeddings │ Cases │ Detection Rules  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Additional Features 

- **Chat-Driven Case Management** — Build cases through natural language. Say "add this to case XYZ" and the system handles findings, activities, timelines, and MITRE tagging. [Learn more](https://vigilsoc.org/docs/chat-case-management/)
- **Detection Engineering** — 7,200+ detection rules (Sigma, Splunk, Elastic, KQL) with coverage analysis, gap identification, and AI-assisted template generation. [Learn more](https://vigilsoc.org/docs/detection-engineering/)
- **Case Management** — Full lifecycle tracking with PDF reports
- **Approval Workflow** — Human-in-the-loop with confidence-based automation (auto-approve above 0.90, require review below 0.85)
- **AI Enrichment** — Automatic threat analysis cached per finding
- **MITRE ATT&CK** — Technique mapping on the ATT&CK tab

## Project Structure

```
vigil/
├── core/              # Shared library: capability domains (findings, cases,
│                      #   llm, integrations, …) over a storage/platform tier
│   └── workflows/definitions/   # WORKFLOW.md definitions (7 built-in)
├── services/          # Deployables only: api (FastAPI), daemon (headless
│                      #   autonomous SOC), worker (ARQ llm-worker)
├── clients/web/       # React + Tailwind frontend
├── tools/mcp/         # MCP servers for Vigil's own services
├── infra/             # Docker Compose, Helm chart, DB init SQL
└── data/schemas/      # JSON validation schemas
```

## Example Usage

### Run a Workflow
```
You: "Run incident response on finding f-20260215-abc123"
Claude: [triage] Severity: Critical — confirmed C2 beaconing from HOST-42
        [investigate] Root cause: phishing email → macro execution → Cobalt Strike beacon
        [respond] Submitted host isolation (confidence 0.96 — auto-approved)
        [report] Incident report generated with MITRE ATT&CK layer
```

### Proactive Threat Hunt
```
You: "Hunt for C2 beaconing activity across all network findings"
Claude: [hunt] Hypothesis: periodic outbound connections to rare destinations
        [network] Found 3 hosts beaconing to 185.220.101.0/24 every 300s
        [malware] Cobalt Strike beacon — extracted 4 IOCs
        [intel] IP attributed to APT28 infrastructure (confidence 0.72)
        [report] Hunt report with 12 IOCs and 3 new detection recommendations
```

### Chat-Driven Case Building
```
You: "Add this to case-20260121-abc123 and note it's part of the kill chain"
Claude: ✓ Added finding to case
        ✓ Logged activity: Part of lateral movement kill chain
        ✓ Tagged with T1021.001 (RDP)

You: "Find similar findings and add them all to this case"
Claude: ✓ Found 3 similar findings via list_findings
        ✓ Added f-002, f-003, f-004 to case
        ✓ Updated timeline with lateral movement progression
```

## Documentation

Guides live on the site at **[vigilsoc.org/docs](https://vigilsoc.org/docs/)**.

| Doc | Contents |
|-----|----------|
| [Agents](https://vigilsoc.org/docs/agents/) | 13 SOC AI agents reference |
| [Integrations](https://vigilsoc.org/docs/integrations/) | MCP integrations — Splunk, CrowdStrike, VirusTotal, 28+ tools |
| [Detection engineering](https://vigilsoc.org/docs/detection-engineering/) | Detection engineering with 7,200+ rules |
| [Chat-driven case management](https://vigilsoc.org/docs/chat-case-management/) | Chat-driven case building guide |
| [Configuration](https://vigilsoc.org/docs/configuration/) | Environment variables, secrets, deployment |
| [Helm](https://vigilsoc.org/docs/helm/) | Chart values, secrets, and install |
| [Contributing](https://vigilsoc.org/docs/contributing/) | How to contribute, DCO |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting, supported versions, disclosure policy |

## Testing with Splunk & Claude

<details>
<summary>Click to expand Splunk testing instructions</summary>

```bash
# Generate 280 realistic security events
python3 scripts/generate_splunk_test_data.py

# Send directly to Splunk
python3 scripts/generate_splunk_test_data.py \
    --send-to-splunk \
    --hec-url https://your-splunk:8088/services/collector \
    --hec-token your-hec-token \
    --no-verify-ssl

# Test full integration (generate → create case → enrich with Claude)
python3 scripts/test_splunk_claude_integration.py \
    --generate-data \
    --create-case
```

**Test data:** 280 events (brute force, malware, C2 traffic, exfiltration, privilege escalation, lateral movement, recon) with full MITRE ATT&CK mappings and realistic IOCs.

See the [Splunk testing guide](https://vigilsoc.org/docs/splunk-testing/) for complete instructions.

</details>

## Export PostgreSQL Data to Splunk

<details>
<summary>Click to expand export instructions</summary>

```bash
# Export everything to Splunk
python scripts/export_postgres_to_splunk.py \
    --hec-url https://your-splunk:8088/services/collector \
    --hec-token your-hec-token \
    --index deeptempo \
    --no-verify-ssl

# Save to file for review first
python scripts/export_postgres_to_splunk.py \
    --save-to-file postgres_export.json
```

**Full Guide:** See the [Postgres to Splunk export](https://vigilsoc.org/docs/postgres-to-splunk/) notes.

</details>

## Contributing

Contributions are welcome! Whether you're fixing bugs, adding new MCP integrations, improving agent prompts, or building new workflows or agents — we'd love your help and leadership.

**Join the community:** Connect with the Vigil community on [Discord](https://discord.gg/Kw68sPJU) to discuss ideas, get help, and collaborate with other contributors.

To contribute:
1. Fork the repo and create a feature branch
2. Make your changes and test them
3. Submit a pull request with a clear description

See the [Quick Start](#quick-start) to get your local environment running, and the [contributing guide](https://vigilsoc.org/docs/contributing/) for the full process.

---

## License

Apache 2.0 — See [LICENSE](LICENSE)

## References

- [Vigil](https://vigilsoc.org/) — Project homepage
- [DeepTempo](https://deeptempo.ai) — Vigil sponsor; LogLM connects via MCP as an AI-native detection layer
- [Model Context Protocol](https://modelcontextprotocol.io/) — MCP specification
- [ATT&CK Navigator](https://mitre-attack.github.io/attack-navigator/) — MITRE visualization
- [SOCBench](https://socbench.org) — Open benchmark for AI in cybersecurity operations

