# Vigil SOC

AI-powered Security Operations Center using Claude and MCP (Model Context Protocol).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Claude (Orchestration)                       │
│   12 Specialized SOC Agents (Triage, Investigator, Hunter...)   │
└─────────────────────────────────────────────────────────────────┘
                              │ MCP Protocol
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      MCP Server Layer                            │
│  DeepTempo Findings │ Approval │ Attack Layer │ 27+ Integrations │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PostgreSQL + pgvector                         │
│         Findings │ Cases │ Embeddings │ AI Decisions            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      DeepTempo LogLM                             │
│   Embeddings │ Anomaly Detection │ MITRE ATT&CK Classification  │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

| Component | Purpose |
|-----------|---------|
| **DeepTempo LogLM** | Detection, embedding generation, MITRE classification |
| **Claude + Agents** | Reasoning, investigation workflows, response orchestration |
| **MCP Servers** | Tool access for findings, cases, integrations |
| **PostgreSQL** | Findings, cases, embeddings, audit logs |

## Navigation

| Page | Path | Purpose |
|------|------|---------|
| Dashboard | `/` | Metrics, recent findings, system status |
| Findings | `/findings` | Security findings, AI investigation |
| Cases | `/cases` | Case management, timelines |
| Timesketch | `/timesketch` | Timeline forensic analysis |
| Settings | `/settings` | All configuration (API, integrations, MCP) |

## Quick Start

1. Configure Claude API: **Settings > Claude API**
2. Set up integrations: **Settings > Integrations**
3. Verify MCP servers: **Settings > MCP Servers**
4. Investigate findings: **Findings > Click any finding > Investigate**

## Data Flow

1. **Ingest**: DeepTempo exports findings with embeddings
2. **Store**: PostgreSQL holds findings, cases, embeddings
3. **Query**: Claude invokes MCP tools to search/filter
4. **Analyze**: Specialized agents guide investigation
5. **Respond**: Approval workflow for containment actions
6. **Document**: Cases, ATT&CK layers, reports

## Documentation

- [CONFIGURATION.md](CONFIGURATION.md) - Environment variables, secrets, setup
- [AGENTS.md](AGENTS.md) - 12 specialized SOC AI agents
- [INTEGRATIONS.md](INTEGRATIONS.md) - Splunk, Timesketch, Cribl, 27+ tools
- [KAFKA_INGESTION.md](KAFKA_INGESTION.md) - Stream findings from Kafka topics
- [FEATURES.md](FEATURES.md) - Cases, approvals, enrichment, reports
- [API.md](API.md) - MCP tool contracts, data models
