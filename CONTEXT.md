# Vigil — `core/` domain structure

How code is grouped under `core/`. The reorg (epic #481) moves loose
`services/*.py` modules into named domain packages so each capability owns its
files and cross-cutting infrastructure has a deliberate home. `core/` has two
tiers: **capability domains** (what the SOC does) and a **shared-infrastructure
tier** (`storage`, `platform`) that capability domains depend on. One section
reaches outside `core/`: the **agent layer**'s vocabulary is here because its
terms collide with the domains' rather than sitting apart from them.

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
[source evidence](https://vigilsoc.org/docs/source-evidence/)). A finding-level concept, not case-scoped.

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

**Workflow** (`workflows`):
The user-facing product noun — a named, multi-agent procedure an analyst runs
from the Workflows screen. Authored as a **Playbook**, executed by **Compose**.
_Avoid_: playbook (that is the artifact, not the product noun)

**Playbook**:
The authored artifact a Workflow compiles to, and the single source of truth for
it — its ordered **Phases**, its per-role directives, its narrative, and the
catalog facts the Workflows screen shows. No schemas, no model or budget
settings: those are the arch and config layers of the three-file split
(arch · playbook · config). Both a `WORKFLOW.md` and a custom workflow's
structured phases are Playbooks. Every other reader derives from it.
_Avoid_: workflow definition, template

**Phase**:
One step of a Playbook — an agent, its instructions, and whether it stops for
approval first. Phases are ordered and the order is authored, not decided: an
agent may appear in more than one Phase, and Compose never reorders or skips
them.
_Avoid_: step, stage

**Compose**:
The run kind that executes a Playbook's phases in order. One of the harness run
kinds beside hunt, investigate and chat.

**Reporting**, **Chat**:
PDF/report generation; the agentic chat loop + durable conversations.

**Auth** (`auth`):
User identity — who the human is and what they may do: authentication, password
policy, cookies, and session/token revocation.
_Avoid_: connector trust (that's **Connector Trust**), permissions

**Connector Trust** (`integrations/extension`):
Which page-extension connector origins Vigil admits into its own page (CSP
`script-src`/`connect-src`, the SSRF guard) and the short-lived tokens minted to
them. A supply-chain trust decision about a third party, not a user login.
_Avoid_: auth, extension auth

**Integration** (`integrations`):
A third-party security product Vigil is configured to talk to. Splits by whether
Vigil carries code for it: a **Catalog Entry** has only a credential form; a
**Vendor Slice** has code. Both are "integrations" to a user; only the second is
one to the codebase.
_Avoid_: connector (that's the page extension), tool, plugin

**Catalog Entry**:
A vendor listed in the Settings UI with a credential form and no Vigil code —
config is stored and read by nothing. Declared once in
`clients/web/src/config/integrations.ts`; the backend derives its secret-field
routing from that declaration rather than restating it.
_Avoid_: integration (unqualified), stub

**Vendor Slice** (`integrations/<vendor>/`):
A code-backed integration: a package owning some of `descriptor`, `ingestion`,
`adapter`, `client`, `tool`, and its router. Every vendor Vigil has code for owns
exactly one slice — there is **no** top-level `tools/` package.

**Integration Descriptor** (`<vendor>/descriptor.py`):
The single source of truth for a Vendor Slice's registry facts — its Integration
Id, its complete field list (not just the secret ones), and the MCP Server Names
it backs. Registries derive from it; they never restate it. Discovered by
scanning `core/integrations/*/descriptor.py`, so a descriptor cannot go dead by
being left out of an import list. A Catalog Entry has no descriptor.

**Integration Id**:
An Integration's persisted identity — primary key of `integration_configs`, key
in `integrations_config.json`, and the stem of each secret's storage name
(`<UPPER_ID>_<FIELD>`). Kebab-case. Renaming one orphans a DB row, a config
entry, and a stored credential at once, so it is a migration, not an edit.
_Avoid_: server name, integration name

**MCP Server Name**:
The key in `mcp-config.json` naming a server *process*, and the prefix on the
tool names that process exposes. Deliberately distinct from Integration Id: one
identifies stored config, the other a running process. One Integration may back
several — Splunk has both an official server and the self-hosted one Vigil ships
— so a descriptor declares a tuple, and CI asserts every name resolves to a real
`mcp-config.json` key.
_Avoid_: integration id, tool name

**Declared Gap**:
A question an investigation never gathered evidence for, carrying why nothing was
gathered. Not a **Verdict** with an empty outcome: it has no activity window,
because there was no activity to bound. Gaps are the common case rather than the
exception, and what ranks an entity up is accumulation of them across
investigations, never being one.
_Avoid_: visibility gap (that is a tool that could not answer), unknown, null result

**Episodic Memory** (`memory`):
What earlier investigations saw and concluded, so a later run stops re-deriving
a settled answer. A hunt's is derived from its **Ledger** at the terminal; a
**Case**'s is derived from the Case itself when it closes, and has no Ledger
behind it at all. Reorders the frontier and never decides (ADR 0015).
_Avoid_: MemPalace (the component this replaced, since removed), cache, RAG

**Distil** (`memory`):
The job that turns a finished investigation into Episodic Memory rows. One per
kind of investigation -- a hunt's reads the Ledger, a Case's reads the closed
Case -- and both poll rather than being told, so a lost signal is a late write
and not a missing one.
_Avoid_: ETL, sync, ingest (those move data; this concludes about it)

**Memory Snapshot** (`memory`):
A frozen copy of **Episodic Memory**, held as a Postgres schema and named by
date. An eval process reads one by putting it on its `search_path`, so the same
hunts can be scored twice without the corpus moving underneath them -- the
**Distil** polls, so live memory grows between two runs and the score moves with
it. An empty one is the control the measurement rests on. Kept and never
overwritten: an old one read by new code measures the code, a new one measures
the system. Covers the exact-join tier only -- narrative search's corpus lives
outside Postgres, so a snapshot freezes half of what a run can recall.
_Avoid_: backup, restore, fixture, baseline, as-of (an as-of filter is what this
replaces; the empty one is a **control**, and _baseline_ belongs to **Golden**)

**Closure Category** (`cases`):
What closing a **Case** determined: `resolved`, `false_positive`, `duplicate`,
`unable_to_resolve`, or `unspecified` for a close that stated none. All but
`duplicate` map to a **Verdict** outcome; `duplicate` writes none. `unspecified`
is a recorded absence, not a determination.
_Avoid_: resolution, disposition, reason

**Entity Key**:
The `type:value` string an **Episodic Memory** row is written and queried on --
`ip:10.0.0.5`, `host:dc01`. Minted by one rule, defang then case-fold, sparing the
types where case is significant, so a stored key and a queried key cannot be
normalised two ways. A `shared_iocs` key is the same shape on a different
vocabulary and is not one of these.
_Avoid_: IOC key, indicator, entity id

**Recall**:
One read of **Episodic Memory** on exact entity keys. A run performs one at start
and renders it into the frozen prefix; a worker performs one mid-run through the
`recall_entity` tool, which lands in the prompt tail and leaves the prefix
undisturbed. Recall never contributes to corroboration — it reorders what to look
at and settles nothing (ADR 0015).

A run's keys come from what the run was opened on. A hunt's are the subjects an
operator declared for the hypotheses actually being put up, and where none were
declared, the entities the hypotheses name. An investigation opened on findings
has neither, so its keys are the entities its trigger findings carry.
Declared keys first because a person typed them and the spec refused the
unusable ones; extraction at all because a scheduled hunt declares none, and the
autonomous path would otherwise never read memory. A key read out of a statement
can be beside the point rather than wrong, which spends prefix budget and, since
recall only reorders, can never move a **Verdict**.
_Avoid_: search, retrieval, lookup (unqualified), context

**Recall Event**:
The **Ledger** record of one run-start **Recall**. A read that was served carries
the keys queried, the rows returned with their provenance, what was dropped and
why, and the selection parameters in force; a read that could not be served is an
**Unavailable Read**, which carries the keys and the reason and none of the rest.
It carries rows and not an order — the order they were
presented in is a fold, so ranking may change without invalidating a historical
Ledger. The parameters are copied in rather than referenced, because a **RunSpec**
records the arch by name and not by version.
_Avoid_: recall log (that is the audit table, which is Python's), snapshot
(unqualified -- a **Memory Snapshot** is the frozen corpus, not this record)

**Unavailable Read**:
The **Recall Event** a run journals when the read could not be served at all --
the far side down, the token wrong, the answer unreadable. It carries the keys
asked about and why, and it is deliberately not an empty **Recall**: empty lists
mean *known-to-be-none*, so recording an outage that way would say those entities
have no history, which is true of every entity while memory is down -- so nothing
would look wrong. A run that opens on one carries no recalled rows for the rest of
its life rather than retrying on a later turn, because a read that succeeded on
turn four would move a prefix the run had already decided against. The run itself
still finishes: **Recall** reorders and never decides, so losing it costs a run an
aid and not an input.
_Avoid_: failed recall, empty recall, **Declared Gap** (that is a question an
investigation left open, not a read that did not happen), **Visibility Gap** (that
is a tool that could not answer about the estate)

**Sighting**:
What one investigation observed about one entity from one source. One row per
entity, investigation and source, so growth tracks hunts and not telemetry
volume. The weaker and truer claim a **Verdict** does not make: *seen during a run
that concluded X*, rather than a subject of that conclusion.
_Avoid_: finding, evidence, observation, hit

**Source Tier**:
What a source *is*: `telemetry` observed our own estate, `feed` asserts about the
world, `not_evidence` is neither. Distinct from **Trust**, which is who
concluded — `analyst` or `agent`. Both sit on a **Verdict**'s sources, and one
does not imply the other: a feed can be cited by an analyst.
_Avoid_: connector trust, confidence, severity

**Stance**:
How one source bore on a **Verdict**: `supports`, `weakens` or `neither`. Replaces
a flat corroborated list, which cannot express direction — a source that weakened
a hypothesis and a source that never bore on it are not the same row.
_Avoid_: corroboration (that is the effect of several sources agreeing, not one
source's direction), confidence, polarity, sentiment

**Trust**:
Who concluded — `analyst` when a person closed it, `agent` when the daemon did.
The other axis on a **Verdict**'s sources, alongside **Source Tier**. Unrelated
to **Connector Trust**, which is about admitting a third-party origin.
_Avoid_: connector trust, source tier, confidence

**Verdict**:
What one investigation concluded about one hypothesis, naming its subjects
rather than every entity its evidence touched (ADR 0016).
_Avoid_: finding, case closure, outcome

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
supervision, autostart config, runtime-config resolution, demo-data seeding,
URL/SSRF safety. Not a junk drawer: a file belongs here only
if it's runtime plumbing with no owning capability. The cut against a capability
domain is **mechanism vs. knowledge**: supervising a process, or resolving a
setting, is `platform`; knowing what the setting *means* is the domain's.

**State Directory**:
The one per-install directory outside the repo holding everything a Vigil
install accumulates that the metadata DB does not — credentials, integration
config, MCP enable flags, detection sources, theme, exports. Defaults to
`~/.vigil`; `VIGIL_DIR` names it explicitly and is the **only** override.
Resolved by `vigil_path()` — a mechanism, so it is `platform` by the cut above.
_Avoid_: config dir, state dir, `.vigil`, "where secrets live", VIGIL_HOME.

**Mirror** vs **Original** (State Directory contents):
A **Mirror** is a State Directory file the DB is authoritative for — the router
reads "database first" and writes the file only "for backward compatibility"
(`theme_config.json`, `s3_config.json`, `integrations_config.json`,
`general_config.json`). Losing one costs nothing. An **Original** has no DB home,
so losing the file loses the data: the secrets store, `mcp_server_enabled.json`,
`detection_sources.json`, `custom_integrations/`. Only Originals constrain where
the State Directory can live.

### Agent layer

Vocabulary of `services/agent/` — TypeScript, outside `core/`, but the terms
collide with the capability domains above often enough to belong beside them.

**Ledger**:
The append-only event log of one run, and its only durable record. Every other
view of a run is derived from it rather than stored beside it. The application
role `vigil_app` may `SELECT` and `INSERT`; `UPDATE`, `DELETE` and `TRUNCATE`
are revoked at the database.
_Avoid_: journal, audit log, history

**Fold**:
The pure function from a Ledger's events to a **Projection** — and by extension
everything derived from one: digest, evidence strength, verdicts, termination,
entities, report.
_Avoid_: reducer, replay (**Replay** is a distinct check — rebuilding a Digest a
decision was shown, to see whether it still matches)

**Projection**:
The state of a run computed by folding its Ledger — hypotheses, questions,
evidence, dispatches, checkpoints. Computed on read, never persisted.
_Avoid_: state, snapshot

**Hypothesis**:
A falsifiable claim a hunt is testing, carrying a status and the **Evidence**
linked for and against it.

**Evidence**:
One observation a worker reported during a run — a summary, a salience, and the
entities it mentions — linked to Hypotheses as supporting or weakening. A
run-scoped concept, distinct from **Source Evidence**, which attaches to a
Finding.
_Avoid_: finding, result, observation

**Visibility Gap**:
Something a run could not see because a tool timed out or was unavailable —
recorded so an absence of evidence is not read as evidence of absence. A refusal
or a bad argument is a defect and must never be recorded as one
(`services/agent/contracts/tool.ts`). Distinct from a **Declared Gap**, which is
a question nobody gathered evidence for rather than a lookup that failed.
_Avoid_: declared gap, error, failure, unknown

**Digest**:
The bounded view of a Projection presented to the lead for a single decision:
recent Evidence, entities seen, open questions. Its sampling is seeded from the
run, so the same Projection yields the same Digest.
_Avoid_: context, prompt

**Golden**:
A recorded output of the implementation being *replaced*, kept as the comparison
target for its port. Its worth is entirely in that provenance: a Golden produced
by the code under test asserts nothing.
_Avoid_: snapshot, baseline, fixture (a fixture is an input; a Golden is an
expected output)

**Fold Equivalence**:
The property the gate asserts — every Fold over a historical Ledger reproduces
its Golden byte-for-byte, projection and derivations alike. Its inputs are the
pre-harness Ledgers under `tests/fixtures/runs/`; a run recorded by current code
is **Replay**'s fixture and not one of these.

**Replay**:
Rebuilding what a decision was shown from the Ledger alone, and checking it
against what was journaled at the time: the **Digest** from the events before the
decision, and the recalled rows from the **Recall Event**. Distinct from **Fold
Equivalence** — that check compares this implementation against the one it
replaced, over a fixture population closed to old-format Ledgers, while a Replay
reads a run this code recorded (`tests/fixtures/replay/`). A Replay that
re-queried **Episodic Memory** rather than reading the journaled rows would read a
neighbourhood that has moved since the run, and pass while showing a decision
something it never saw.
_Avoid_: fold, re-run, regression test (a regression snapshot is the fixture; the
Replay is the check)

### Console (web client)

Vocabulary of `clients/web/src/` — TypeScript, outside `core/`, and here for the
same reason the agent layer is: it re-declares the domains' nouns as its own view
shapes, so the collisions need naming rather than leaving to the reader.

**Console** (SOC Console):
The authenticated surface an analyst works in — nav rail, topbar, screen area and
the Vigil chat dock, all under one `.soc-console` root. Login and Setup are
full-page surfaces that render *outside* it.
_Avoid_: dashboard (that is one Screen), app, UI, redesign (retired — the console
was "the redesign" only while a second UI existed, and that ended with #502)

**Screen**:
One of the eight named views the Console routes, each owning a URL (`/<screen>`)
and implementing the `ConsoleScreenProps` contract the shell passes it. Login,
Setup and the in-shell 404 are views but not Screens: nothing routes them by key
and none implements the contract.
_Avoid_: page, tab, view

## Relationships

- A **Case** groups one or more **Findings**
- **Episodic Memory** derives **Verdicts** from a **Ledger** after a hunt
  terminates, and from a **Case** when it closes; it never writes during a run,
  and a run reads it once at start (ADR 0015)
- Closing a **Case** writes one **Verdict**, whose Trust is `analyst` when a
  person closed it and `agent` otherwise; reopening the Case withdraws it
- A **Verdict**'s sources each carry a **Source Tier** and the Verdict carries
  one **Trust**. The two are independent axes: **Trust** is who concluded, and
  a `feed`-tier source can be cited by an `analyst`
- **Source Tier** is stamped at write time, never joined at read time, so
  recategorising an **Integration** later cannot change how a past **Verdict**
  was corroborated
- **Trust** is unrelated to **Connector Trust**, which is about admitting a
  third-party origin rather than weighing a conclusion
- A **Recall** returns **Sightings**, **Verdicts** and **Declared Gaps** in one
  shape, carried two ways: journaled verbatim as the **Recall Event** at run
  start, and returned as the single row of a `recall_entity` tool result mid-run.
  One result shape and not two — a second copy of it is a second contract, and the
  second one drifts. An **Unavailable Read** is not that second copy: it is the
  account of a read that did not happen, which only the harness ever writes.
  Declared in `core/memory/recall_contract.py` and
  `services/agent/contracts/memory.ts`, with a ratchet that fails when they
  disagree
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
- The agent layer has exactly **two ways in**, and they are what it splits along
  rather than by protocol: the **run queue** (`agent-runs`, enqueued by
  `core/agents/queue.py`) and the **agent HTTP surface** (chat and run
  projections). **Agent Worker** drains the first, **Agent Serve** answers the
  second; every HTTP route the layer exposes is request-driven, so projections
  ride with chat rather than earning a third deployable
- A **Workflow** is authored as exactly one **Playbook** and executed by
  **Compose**; a Playbook holds one or more ordered **Phases**, and a **Phase**
  names exactly one agent
- The **Playbook** is the registry for a Workflow: the Workflows catalog, the
  phase sequence and the per-role prompts all derive from it and none restates
  it — the same rule ADR-0001 sets for the **Integration Descriptor**
- An **Integration** is either a **Catalog Entry** or a **Vendor Slice**, never
  both; only a Vendor Slice has an **Integration Descriptor**
- An **Integration Descriptor** declares exactly one **Integration Id** and zero
  or more **MCP Server Names**; the secret registry, the bridge's server map, and
  `mcp-config.json` all derive from it rather than restating it
- A **Vendor Slice** lives only under `core/integrations/<vendor>/`. The
  top-level `tools/` package holds no vendor servers — `tools/mcp/` is a separate
  thing: servers that talk to Vigil's *own* services, which are not Integrations
- A **Ledger** folds to exactly one **Projection**, and the Projection is never
  stored — there is no second copy of a run's state to drift
- A **Digest** is derived from a **Projection** for one lead decision; **Evidence**
  links to **Hypotheses** as supporting or weakening
- A **Golden** is the output of the implementation being replaced, never of the one
  under test. This is the whole of **Fold Equivalence**'s value and the rule
  ADR 0012 exists to hold
- The **Console** routes one or more **Screens**; a Screen is named by exactly one
  `ConsoleScreenKey` and reached at exactly one URL
- A **Screen** renders a **Finding view-model**, not a **Finding** — the mapping
  between them lives in one place (`src/data/mappers.ts`) and is lossy in both
  directions

## Flagged ambiguities

- **"workflow" meant three things.** The five `WORKFLOW.md` definitions, the
  DB-authored custom workflows built in `WorkflowBuilder.tsx`, and the
  TypeScript control-flow modules under `services/agent/workflows/` were all
  "workflows", and `CONTEXT.md` itself glossed the domain as "multi-agent
  playbooks" — making "playbook" an informal synonym. #624 spent that word on a
  precise concept, so the synonym is withdrawn. Resolved: **Workflow** is the
  product noun, **Playbook** is the authored artifact, **Compose** is the run
  kind that executes one. A Workflow is authored as a Playbook and executed by
  Compose.

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
- **"integration id" meant five different strings.** `mcp-config.json` keys,
  descriptor `id`, descriptor `mcp_server_name`, the frontend catalog `id`, and
  the snake_case key each `tools/*.py` passed to `get_integration_config()` all
  drifted apart. The damage was not cosmetic: six of the eleven `tools/` servers
  read a snake_case key the UI never writes, so their config resolved to `{}`;
  every `"<x>-server"` value in `INTEGRATION_TO_SERVER_MAP` matched no
  `mcp-config.json` key, so the bridge's dedupe never fired and it fell through to
  spawning `python -m tools.<name>` for modules #484 had already moved. Resolved:
  exactly two identifiers survive — **Integration Id** (persisted, kebab-case) and
  **MCP Server Name** (process). They stay separate because they identify
  different things, and CI asserts each descriptor's server name resolves to a
  real `mcp-config.json` key. No id is renamed: renaming is a data migration, and
  the drift is fixable without one.
- **`tools/` vs `core/integrations/`.** #484/#557 moved the ten vendors that had a
  slice to consolidate and left eleven single-file MCP servers behind, so location
  encoded "did this vendor have other files" rather than any real distinction.
  Resolved: **Vendor Slice** is the only home — every code-backed vendor owns
  `core/integrations/<vendor>/`, and the top-level `tools/` package is retired.
- **"evidence" and "finding" each meant two things.** `Finding` and **Source
  Evidence** are ingest-side and finding-level; the hunt's **Evidence** is a
  worker's observation inside a run and never attaches to a Finding. The worker
  prompts make it worse by calling their own output a "finding" ("nothing matched
  is a real finding"). Resolved: the two Evidence concepts are distinct and stay
  distinct — the run-scoped one is **Evidence**, the finding-scoped one is
  **Source Evidence**, and a worker's prose calling its output a finding is loose
  usage, not the term.
- **"golden" read as "saved output".** Treated that way it invites the one thing
  that voids it — generating goldens from the code under test, which produces a
  green suite pinning a port to itself. Resolved: a **Golden** is defined by
  provenance, not by being saved; regenerating one means re-running the *original*
  implementation. See ADR 0012.
- **the web client's `Finding` is not the domain's `Finding`.** The console
  declares its own `Finding` and `CaseRow` interfaces, and `src/data/mappers.ts`
  says plainly that "the view shapes carry richer fields than the API returns" —
  so a field visible on a Finding in the UI may be derived, defaulted to an
  em-dash, or a neutral placeholder rather than anything the backend sent.
  Resolved: they are distinct, and the UI one is a **Finding view-model**. Reading
  a screen as evidence of what a Finding *is* gets the domain wrong; `mappers.ts`
  is the only honest account of which fields survive the trip.
- **"a future run can be added" is narrower than it reads.** A new fixture is only
  a valid Fold Equivalence input if its Ledger is in the pre-harness file format,
  and nothing produces that format any more. The population of possible fixtures
  is therefore closed — the ten committed runs plus whatever old-format ledgers
  still exist. A hunt run by current code can be a regression snapshot, never a
  Golden.
- **`type:value` keys are minted by two rules that disagree.** An **Entity Key**
  (`core/memory/entity_keys.py`) defangs and keeps case for `arn` and `aws_key`; a
  `shared_iocs` key (`core/storage/shared_ioc_repository.py`) does neither, and
  spells a host `hostname` where memory spells it `host`. Handing one subsystem's
  keys to the other returns no rows, which reads as an entity nobody has looked
  at rather than as a bad query. Resolved: they stay separate, because unifying
  them is a migration of a live table. The one shared piece is the
  `entity_context` spelling map, which yields candidates in memory's vocabulary --
  `make_key` aliases `host` back to `hostname` on the way in, so the older keys
  are unchanged.
