# Vigil

Vigil is the open-source AI SOC where your playbooks are plain-text files, your agent logic is readable Python, and your integrations use an open standard ([MCP](https://modelcontextprotocol.io/)). Every proprietary AI SOC on the market is a black box you rent. Vigil is a capability you own.

The project is built on three pillars: **Agents** — 12 specialized AI agents you can read, fork, and rewire; **Workflows** — multi-agent playbooks defined as Markdown files you edit directly; and **Integrations** — 30+ tool connections via MCP that you configure, not a vendor. The most important pillar is **YOU** — this is your project. Contribute via feedback, code, a repo star, memes on Discord, or otherwise.

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

Workflows are the operational core of Vigil. Each worfklow chains multiple specialized AI agents into an end-to-end playbook that executes with a single command. No manual hand-offs, no copy-pasting between tools — the agents coordinate automatically.  

| Workflow | Agents | What It Does |
|----------|--------|-------------|
| **Incident Response** | Triage → Investigator → Responder → Reporter | NIST IR framework: triage an alert, investigate root cause, contain the threat, produce an audit-ready report |
| **Full Investigation** | Investigator → MITRE Analyst → Correlator → Responder → Reporter | Deep-dive with ATT&CK mapping, cross-signal correlation, response planning, and comprehensive documentation |
| **Threat Hunt** | Threat Hunter → Network Analyst → Malware Analyst → Threat Intel → Reporter | Hypothesis-driven hunting across network, endpoint, and threat intel — with IOC enrichment and detection recommendations |
| **Forensic Analysis** | Forensics → Malware Analyst → Network Analyst → Reporter | Post-incident digital forensics with evidence preservation, chain-of-custody documentation suitable for legal proceedings |

**How it works:** Say `"Run incident response on finding f-20260215-abc123"` and the system sequences four agents — triage scores the alert, investigator digs into root cause, responder submits containment actions with confidence-based approval, and reporter generates the final documentation.

Workflows are defined as `WORKFLOW.md` files in the `workflows/` directory and are fully customizable. Create your own by defining the agent sequence, tools used, and phase-by-phase instructions.

```
workflows/
├── incident-response/WORKFLOW.md
├── full-investigation/WORKFLOW.md
├── threat-hunt/WORKFLOW.md
└── forensic-analysis/WORKFLOW.md
```

### Create Your Own Workflow in 60 Seconds

Every workflow is a Markdown file. Here's what one looks like inside:

```markdown
---
name: phishing-triage
description: "Triage and investigate phishing reports from user submissions."
agents:
  - triage
  - investigator
  - responder
tools-used:
  - get_finding
  - list_findings
  - nearest_neighbors
  - create_approval_action
use-case: "A user reports a suspicious email and the SOC needs to assess, investigate, and contain."
trigger-examples:
  - "Run phishing triage on finding f-20260401-abc123"
  - "Investigate this phishing report"
---

# Phishing Triage Workflow

## Phase 1: Assess the Report (Triage Agent)
Fetch the finding, extract sender/domain/URLs, score severity, check for known-bad indicators.

## Phase 2: Investigate (Investigator Agent)
Use nearest_neighbors to find similar reports. Correlate with detection rules. Build an evidence timeline.

## Phase 3: Contain (Responder Agent)
If confirmed malicious: block sender domain, quarantine matching emails, submit approval actions with confidence scores.
```

Edit this file. That's it. No vendor ticket, no professional services, no YAML/JSON schema to learn.

Scaffold a new workflow instantly with the CLI:

```bash
python scripts/create_workflow.py phishing-triage
# creates workflows/phishing-triage/WORKFLOW.md with a commented template
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
| **Core** | DeepTempo Findings, Approval, ATT&CK Layer, Tempo Flow | Built-in SOC operations |

**Coming soon:** AWS Security Hub, Azure Sentinel, GCP Security, Okta, Microsoft Defender, SentinelOne, Carbon Black, PagerDuty.

MCP servers live in `mcp-servers/` and are configured via the Settings UI or `mcp_config.json`. Add a new integration by dropping an MCP server into the `tools/` directory — or use the built-in Custom Integration Builder to generate one from API docs.  If you build an integration that you find useful, chances are someone else will as well.  Please contribute!

---

## Quick Start

```bash
git clone --recurse-submodules https://github.com/Vigil-SOC/vigil.git
cd vigil
./start_web.sh
```

> **Note:** Docker must be running before you start. The startup script handles everything else: creates the Python virtual environment, installs dependencies, starts PostgreSQL, initializes the database with a default admin user, installs frontend packages, and launches both backend and frontend servers.

Auth bypass is enabled by default (`DEV_MODE=true`) for quick development. Full auth is WIP and while it will turn on it is untested. To activate auth set `DEV_MODE=false`.

### Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for frontend)
- **Docker Desktop** (must be running — used for PostgreSQL)
- **Git** (with submodule support)
- An LLM provider key. Vigil supports Anthropic Claude (default), OpenAI, and Ollama (local) — configure providers in Settings → AI Config. See [`docker/bifrost/README.md`](docker/bifrost/README.md) for the multi-provider gateway. *(optional for initial testing)*

### Default Login Credentials

| | |
|---|---|
| **Username** | `admin` |
| **Password** | `admin123` |

> Change these in production!

### Manual Install

<details>
<summary>Click to expand manual setup steps</summary>

```bash
# Clone with submodules
git clone --recurse-submodules https://github.com/Vigil-SOC/vigil.git
cd vigil

# If you already cloned without --recurse-submodules:
git submodule update --init --recursive

# Environment (DEV_MODE enabled by default)
cp env.example .env
# Edit .env and add your ANTHROPIC_API_KEY (optional for testing)

# Backend setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Frontend setup
cd frontend
npm install
cd ..
```

</details>

### Install on Kubernetes

A production-style Helm chart lives at [`helm/vigil/`](helm/vigil/):

```bash
helm install vigil ./helm/vigil \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set secrets.postgresPassword="$(openssl rand -hex 24)" \
  --set secrets.jwtSecretKey="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
```

See [docs/HELM.md](docs/HELM.md) for the full values reference, external
Postgres/Redis setup, ingress configuration, and troubleshooting.

### Run

**Option A: All-in-one (recommended)**

```bash
# Interactive mode (keeps terminal attached, Ctrl+C to stop)
./start_web.sh

# OR background mode (frees terminal)
./start_daemon.sh
```

**Option B: Manual (separate terminals)**

```bash
# Terminal 1: Start database (Docker must be running)
cd docker && docker-compose up -d postgres

# Terminal 2: Initialize admin user and generate demo data
source venv/bin/activate
python scripts/init_default_user.py
python scripts/demo.py

# Terminal 3: Start backend
source venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
uvicorn backend.main:app --host 127.0.0.1 --port 6987 --reload

# Terminal 4: Start frontend
cd frontend && npm run dev
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

```bash
cd docker && docker-compose up -d
```

Starts PostgreSQL, Backend API, and SOC Daemon.

### Run SOC Daemon (Headless Mode)

For autonomous 24/7 monitoring without the UI:

```bash
source venv/bin/activate
python daemon/main.py
```

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
│                   12 Specialized AI Agents                        │
│  Triage │ Investigator │ Hunter │ Correlator │ Responder │ ...   │
└──────────────────────────────────────────────────────────────────┘
                │                              │
     Agent SDK (19 tools)              MCP (100+ tools)
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

- **Auto-Contributor** — Automated competitive research against proprietary AI security platforms. Analyzes a vendor's capabilities, maps gaps versus Vigil and the open-source ecosystem, and generates ready-to-file GitHub issues with acceptance criteria. The goal: make Vigil a superset of every proprietary AI SOC, one contribution at a time. See [`contrib/auto-contributor/`](contrib/README.md)
- **Chat-Driven Case Management** — Build cases through natural language. Say "add this to case XYZ" and the system handles findings, activities, timelines, and MITRE tagging. [Learn more](docs/CHAT_CASE_MANAGEMENT.md)
- **Detection Engineering** — 7,200+ detection rules (Sigma, Splunk, Elastic, KQL) with coverage analysis, gap identification, and AI-assisted template generation. [Learn more](docs/DETECTION_ENGINEERING.md)
- **Case Management** — Full lifecycle tracking with PDF reports
- **Approval Workflow** — Human-in-the-loop with confidence-based automation (auto-approve above 0.90, require review below 0.85)
- **AI Enrichment** — Automatic threat analysis cached per finding
- **MITRE ATT&CK** — Technique mapping and Navigator layer visualization

## Project Structure

```
vigil/
├── workflows/         # WORKFLOW.md definitions (4 built-in)
├── contrib/           # Community tools: auto-contributor, benchmarking
├── mcp-servers/       # MCP server implementations (30+)
├── backend/           # FastAPI backend API + Agent SDK tools
├── frontend/          # React + MUI frontend
├── services/          # Business logic (workflows service, etc.)
├── daemon/            # Headless autonomous SOC service
├── tools/             # Additional tool implementations
├── database/          # PostgreSQL models and migrations
├── core/              # Config, rate limiting, exceptions
├── docker/            # Docker Compose setup
├── docs/              # Documentation
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
Claude: ✓ Found 3 similar findings via embedding search
        ✓ Added f-002, f-003, f-004 to case
        ✓ Updated timeline with lateral movement progression
```

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/AGENTS.md](docs/AGENTS.md) | 12 SOC AI agents reference |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | MCP integrations — Splunk, CrowdStrike, VirusTotal, 28+ tools |
| [docs/DETECTION_ENGINEERING.md](docs/DETECTION_ENGINEERING.md) | Detection engineering with 7,200+ rules |
| [docs/CHAT_CASE_MANAGEMENT.md](docs/CHAT_CASE_MANAGEMENT.md) | Chat-driven case building guide |
| [docs/CHAT_CASE_QUICK_REFERENCE.md](docs/CHAT_CASE_QUICK_REFERENCE.md) | Quick reference for chat commands |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Environment variables, secrets, deployment |
| [docs/FEATURES.md](docs/FEATURES.md) | Cases, approvals, enrichment |
| [docs/API.md](docs/API.md) | MCP tool contracts, data models |
| [docs/README.md](docs/README.md) | Architecture overview |
| [docs/SPLUNK_TESTING_GUIDE.md](docs/SPLUNK_TESTING_GUIDE.md) | Splunk test data and integration testing |
| [contrib/auto-contributor/](contrib/auto-contributor/SKILL.md) | Competitive research and contribution planning tool |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute, auto-contributor workflow, DCO |

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

See the [Splunk Testing Guide](docs/SPLUNK_TESTING_GUIDE.md) for complete instructions.

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

**Quick Start:** See [POSTGRES_TO_SPLUNK_QUICKSTART.md](POSTGRES_TO_SPLUNK_QUICKSTART.md)
**Full Guide:** See [docs/POSTGRES_TO_SPLUNK_EXPORT.md](docs/POSTGRES_TO_SPLUNK_EXPORT.md)

</details>

## Contributing

Contributions are welcome! Whether you're fixing bugs, adding new MCP integrations, improving agent prompts, or building new workflows or agents — we'd love your help and leadership.

**Find meaningful work automatically:** Vigil includes an [auto-contributor](contrib/README.md) tool that researches proprietary AI security platforms, identifies capability gaps, and generates ready-to-file GitHub issues. Pick a vendor, run the tool, and you'll have a scoped contribution spec in minutes.

**Join the community:** Connect with the Vigil community on [Discord](https://discord.gg/Kw68sPJU) to discuss ideas, get help, and collaborate with other contributors.

To contribute:
1. Fork the repo and create a feature branch
2. Make your changes and test them
3. Submit a pull request with a clear description

See the [Quick Start](#quick-start) to get your local environment running, and [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

---

## License

Apache 2.0 — See [LICENSE](LICENSE)

## References

- [Vigil](https://vigilsoc.org/) — Project homepage
- [DeepTempo](https://deeptempo.ai) — Vigil sponsor; LogLM connects via MCP as an AI-native detection layer
- [Model Context Protocol](https://modelcontextprotocol.io/) — MCP specification
- [ATT&CK Navigator](https://mitre-attack.github.io/attack-navigator/) — MITRE visualization
