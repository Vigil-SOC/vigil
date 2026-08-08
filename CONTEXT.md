# Vigil — `core/` domain structure

How code is grouped under `core/`. The reorg (epic #481) moves loose
`services/*.py` modules into named domain packages so each capability owns its
files and cross-cutting infrastructure has a deliberate home. `core/` has two
tiers: **capability domains** (what the SOC does) and a **shared-infrastructure
tier** (`storage`, `platform`) that capability domains depend on.

## Language

### Capability domains

**Finding**:
The atomic unit of security signal — one detection/alert instance ingested into
Vigil. Findings are finding-level (evidence, entity graphs, MITRE predictions
attach to a Finding), distinct from the Case that groups them.
_Avoid_: alert, event (when you mean a Finding specifically)

**Case**:
An investigation container that groups Findings with evidence, IOCs, SLA, and a
lifecycle. `cases` owns case lifecycle + anything that writes into a Case
(e.g. sandbox reports correlated into case evidence/IOCs).
_Avoid_: incident, ticket

**Source Evidence**:
Normalized, bounded evidence attached to a Finding (contract in
`docs/SOURCE_EVIDENCE.md`). A finding-level concept, not case-scoped.

**Detection** (`detections`):
Detection-*rule* sources and their management — not finding analysis. "The rules
that produce Findings," distinct from the Findings themselves.

**Response** (`response`):
Autonomous containment actions and the approval workflow that gates them.

**Threat Intel** (`threat_intel`):
External threat knowledge — STIX/TAXII feed ingestion and MITRE ATT&CK taxonomy
resolution. MITRE lookup lives here as a reusable taxonomy resolver.

**Ingestion** (`ingestion`):
Normalizing *external* security data into Findings (SIEM, Kafka, S3-dropped
findings) — *what* a source yields and how it becomes a Finding. Distinct from
`storage`: ingestion *uses* storage clients; storage never depends on ingestion.

**Federation** (`federation`):
The scheduled poll loop that *drives* ingestion sources — the `federation_sources`
registry, per-adapter loops, cursors, and the global on/off toggle. Ingestion is
*what* a source yields; Federation is *when and how often* Vigil asks for it.
A vendor slice carries both: `ingestion.py` subclasses `SIEMIngestionService`,
and `adapter.py` wraps that service to satisfy the Federation contract.
_Avoid_: polling, sync, multi-tenancy (it is not Vigil-to-Vigil federation)

**Workflows**, **Reporting**, **Chat**:
Multi-agent playbooks; PDF/report generation; the agentic chat loop + durable
conversations.

**Auth** (`auth`):
User identity — who the human is and what they may do: authentication, password
policy, cookies, and session/token revocation.
_Avoid_: connector trust (that's **Connector Trust**), permissions

**Connector Trust** (`integrations/extension`):
Which page-extension connector origins Vigil admits into its own page (CSP
`script-src`/`connect-src`, the SSRF guard) and the short-lived tokens minted to
them. A supply-chain trust decision about a third party, not a user login.
_Avoid_: auth, extension auth

### Shared-infrastructure tier

**Storage** (`storage`):
How Vigil persists and reads *its own* data — the full metadata-DB layer (ORM
`models`, the engine/session in `connection`, `DatabaseService`, the DB-backed
`config_service`), the higher-level data-access layer, DB/connection proxies, and
the S3 object-store client. A capability domain may depend on `storage`;
`storage` depends on no capability domain. There is **no** top-level `database/`
Python package and **no** `core/platform/db/` — all DB code lives here.

**Platform** (`platform`):
Process/config/runtime plumbing — local service orchestration and process
supervision, autostart config, runtime-config resolution, memory-palace paths,
demo-data seeding, URL/SSRF safety. Not a junk drawer: a file belongs here only
if it's runtime plumbing with no owning capability. The cut against a capability
domain is **mechanism vs. knowledge**: supervising a process, or resolving a
setting, is `platform`; knowing what the setting *means* is the domain's.

## Relationships

- A **Case** groups one or more **Findings**
- **Ingestion** produces **Findings** and depends on **Storage** (never the reverse)
- **Federation** drives **Ingestion** (an adapter wraps an ingestion service);
  Ingestion never depends on Federation
- Capability domains depend on the **Storage**/**Platform** tier; the tier
  depends on no capability domain. This is no longer prose: `.importlinter`
  enforces it, plus "core must not import the deployables", on every PR with
  no exemptions. The rule had stood since R5 and accumulated 20 live
  counterexamples by R9, which is the argument for a gate over a convention.
- **LLM** code (`core/llm/`, in flight as #485/#522) is a separate slice, not
  part of these domains
- The **LLM gateway** (`core/llm/gateway`) enqueues LLM jobs onto the `arq:llm`
  queue; the **worker** (`services/worker`) is the sole consumer that executes
  them — the enqueue/execute seam between `core/` and the `services/` deployables

## Flagged ambiguities

- **"finding" work kept falling into `cases`.** `source_evidence` and
  `graph_builder` are finding-level, not case-level. Resolved: they belong to a
  **`findings`** domain, deferred until PR #537 (`services/findings/enrichment/`,
  issue #470) lands, then consolidated into `core/findings/` in a follow-up.
  Until then both stay in `services/`.
- **`platform` was absorbing LLM config.** `defaults.py` and `runtime_config.py`
  read as "central config" but their content is model/thinking/AI-ops settings.
  Resolved: they're **LLM-slice** files (#485), not `platform`. `defaults.py`
  (`DEFAULT_MODEL`, `build_thinking_kwargs`) now lives at `core/llm/defaults.py`
  — moved with the worker slice (#508), which also killed the `core/llm/gateway`
  → `services.defaults` inversion. **Amended (R9):** `runtime_config.py` cannot
  "stay in `services/`" — `services/` now means deployables only (`api`, `daemon`,
  `worker`). Re-resolved by the mechanism-vs-knowledge cut: it is a DB > env >
  default resolver with a TTL cache — a *mechanism* — so it lands at
  `core/platform/runtime_config.py`, not `core/llm/`. Its keys are LLM-ops; the
  resolver is not. This also removes the last `core.chat`/`core.llm` →
  `services.*` inversions.
- **`platform` ↔ `llm` was a cycle, not a violation (R9).** `core/platform/
  service_manager.py` and `core/llm/providers/ollama.py` imported each other
  across the tier boundary — 7 edges, every one a function-local import deferred
  purely to dodge the cycle. Resolved: `ollama.py` is misfiled. Its docstring
  calls it a "Host-native Ollama supervisor"; it implements `service_manager`'s
  own `ServiceSpec`/`ServiceStatus`/`ActionResult` protocol, and `service_manager`
  is its *only* consumer — nothing in `core/llm/` imports it. It moves to
  `core/platform/ollama_supervisor.py`, deleting the cycle and letting the
  remaining imports return to module top-level. Supervising the Ollama process is
  platform's "local service orchestration"; the payload being LLM traffic doesn't
  make the supervision LLM knowledge.
- **`s3_service`: ingestion or storage?** Its purpose is sourcing findings, but
  `storage`'s own data-access layer depends on it. Resolved: **storage** (an
  object-store client), so the layering isn't inverted.
- **DB code: `platform/db/` or `storage`?** REARCHITECTURE §7 routed the
  remaining top-level `database/*.py` (models, connection, service,
  config_service) to `core/platform/db/`. Resolved (R6, epic #481): they join
  **`core/storage/`** — storage already owned the data-access layer + `db_proxy`,
  and a `platform/db/` split would only relocate the cross-domain reach
  (`core/storage/database_data_service` → top-level `database`) instead of killing
  it. No `core/platform/db/`; the top-level `database/` package is retired and its
  SQL moves to `infra/database/init/`.
