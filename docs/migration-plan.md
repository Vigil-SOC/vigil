# Migration plan: the TypeScript agent layer into vigil main

Status: draft for review
Date: 2026-08-07
Companion documents: `CONTEXT.md` (glossary), `docs/adr/0001`–`0004`,
`docs/plan.md` (hunt design), `docs/AI Agent Architecture.pdf` (target modules)

---

## 1. What this migration actually is

Not a merge. `git diff --stat main threat-hunt` is 755 files changed, 15,391
insertions, 191,755 deletions. The `threat-hunt` branch is 53 files with zero
Python; it shares git history with main and nothing else. There are no
features to merge into main's loop — main's loop is being deleted and its job
is being rebuilt in TypeScript.

Three corrections to the existing proposal, which other readers will
otherwise carry forward:

**The AI surface is larger than estimated.** `docs/plan.md:676` puts it at
14,500 lines. Measured on main:

| Group | Lines |
|---|---|
| `services/claude_service.py` | 3,671 |
| `backend/api/claude.py` | 1,720 |
| `daemon/agent_runner.py` | 1,643 |
| `services/workflows_service.py` | 1,321 |
| `daemon/orchestrator.py` | 1,320 |
| `services/model_registry.py` | 1,059 |
| `services/openai_agent_service.py` | 842 |
| `services/llm_router.py` | 749 |
| `services/soc_agents.py` | 733 |
| `services/llm_worker.py` | 711 |
| 15 others (mcp, tool_manager, cost, chat, transport, workdir) | 3,083 |
| **AI surface** | **16,852** |
| `tools/*.py` — 20 vendor tools | **3,278** |
| **Total in scope** | **~22,130** + the `mcp-servers` submodule |

**"Five loops to one" counts two different things.** Main's `agent_runner`
has an outer investigation loop (50 iterations) *and* an inner tool loop (25
turns, hardcoded). The target is **one harness loop and one workflow loop per
agent type**, not one loop. See ADR-0002.

**`ai/loop.py ~400` is the harness, not the hunt controller.** The
architecture PDF's flowchart for it is `provider.stream → tool calls? →
approval required? → dispatch → security wrap → write state → terminal
status? → budget ok?`. The hypothesis loop is a separate diagram under the
heading "Nuanced deterministic workflow attached to general harness". The
hunt controller's 1,803 lines are not a 4.5x overrun on a 400-line budget;
they are a different layer.

One precondition in `docs/plan.md:329` is unmet and should be struck: it
assumes a merged `core/llm/` neutral loop and repository/UoW seams. `core/`
on main is eight files — config, exceptions, rate limiting, secrets,
telemetry. Nothing to build on.

---

## 2. Decisions locked

| # | Decision | Where argued |
|---|---|---|
| D1 | The AI layer is TypeScript; the Python backend stays | ADR-0001 |
| D2 | TypeScript owns its own Postgres tables and is their single writer | ADR-0001 |
| D3 | Domain-free harness + one workflow module per agent type | ADR-0002 |
| D4 | All four architecture-doc seams built up front: `memory`, `budget`, `state`, `tools/dispatch` | this doc, §4 WS-A |
| D5 | Workers are leaves. Topology is fan-out width only; no nested loops, no peer leads | `CONTEXT.md`, §Agents |
| D6 | Ledger authoritative for control checkpoints; `approval_actions` is an outbound mirror | ADR-0003 |
| D7 | Owned query port per SIEM, native dialect, per-dialect deny-by-default allow-list | ADR-0004 |
| D8 | Tools reimplemented in TypeScript; any MCP server registers through the same tool port, allow-listed per role | ADR-0004 |
| D9 | One append-only `hunt_events` table, `payload`/`snapshot` split, PK `(run_id, seq)` | §3 |
| D10 | DDL lives in `database/init/*.sql`; TypeScript never issues DDL | §3 |
| D11 | BullMQ on the existing Redis; FastAPI enqueues plain JSON | ADR-0001 |
| D12 | Context/caching layer built into the kernel from the start | §4 WS-A |
| D13 | One Postgres rate table read by both languages | §4 WS-E |
| D14 | Code lives in `ai/` with its own `package.json` | §4 WS-E |
| D15 | Build order: kernel → hunt → chat → compose → investigate | §5 |
| D16 | `investigate` keeps the existing API contract; `/files` rendered from the ledger | §5 |
| D17 | Existing `runs/*.jsonl` become a fold-equivalence regression gate | §4 WS-B |

---

## 3. Persistence

One append-only table, because the projection must stay derived:

```sql
CREATE TABLE hunt_events (
  run_id         uuid        NOT NULL,
  seq            integer     NOT NULL,
  ts             timestamptz NOT NULL DEFAULT now(),
  kind           text        NOT NULL,
  payload        jsonb       NOT NULL,
  snapshot       jsonb,
  schema_version integer     NOT NULL,
  PRIMARY KEY (run_id, seq)
);
```

The composite primary key **is** the single-mutator guarantee: a concurrent
second writer gets a unique violation rather than silently interleaving. That
is stronger than the row lease alone, and the lease remains for liveness
(reaping a crashed holder), not for correctness.

`snapshot` holds the digest presented to the lead. It is selected only by
replay, never by the fold — measured on the demo ledger, decision events
reach 56.7 KB each and an 8-iteration run is 956 KB against a 5-iteration
run's 247 KB. Without the split, folding at iteration 40 would read tens of
megabytes to build one digest.

Two changes to what is journaled today:

- **Token counts.** The ledger currently records `cost_usd` only; token
  counts are collapsed into dollars at the call site. Journal `input`,
  `output`, `cache_read` and `cache_write` on every decision and dispatch.
  Without this, a run cannot be re-priced and the caching work in WS-A cannot
  be measured against a baseline.
- **Run kind.** Events carry which workflow produced them, so one table
  serves hunt, investigate and compose.

DDL ships as `database/init/18_agent_ledger.sql` alongside the existing
seventeen files, which Helm already copies. One migration system, no new ops
surface.

---

## 4. Workstreams

Six streams. **Phase 0 fixes the five contracts they share**; after that they
proceed independently.

### Phase 0 — contracts (blocking, do first, one sitting)

Nothing else starts until these five are written down and agreed. They are
small, and every one of them is a hard dependency between two streams.

1. **`Tool` port** — signature, error shape, row/timeout bounds. (WS-A ↔ WS-C)
2. **Event kinds + `hunt_events` DDL** — the payload discriminant. (WS-B ↔ WS-D)
3. **`Budget` / shared `Spend`** — how lead, workers and critic draw on one
   pool, and what a refusal looks like. (WS-A ↔ WS-D)
4. **BullMQ job payload** — what FastAPI enqueues, what the worker resumes
   from. (WS-B ↔ WS-E ↔ backend)
5. **Rate table schema** — read by TypeScript at startup, owned by the DDL.
   (WS-E ↔ WS-A)

### WS-A — Kernel / harness

`ai/core/`. The tool loop per the architecture PDF's flowchart, with statuses
`running | waiting_approval | completed | failed`. `provider.ts` +
`wire.ts` (one provider, two surfaces: OpenAI `/v1` and `/anthropic` for
thinking and `cache_control`). Tool registry, security (injection scanning,
result wrapping), approval. The four seams as injected ports: `memory`,
`budget`, `state`, `tools/dispatch`.

`context.ts` implements the PDF's page-7 layout as the *only* way a request
is assembled — system prompt frozen byte-identical, tool schemas serialised
with sorted keys in stable order, memory recall rendered once then persisted,
an explicit cache breakpoint, transient tail re-rendered and never persisted.
This is the largest cost lever available: the worker system prompt plus the
full schema dump is byte-identical across every tool turn and every worker,
and is currently re-billed at full rate each time. It is built in from the
start because retrofitting byte-stability means auditing every call site.

Recommendation carried forward for confirmation: ship `memory` as a port with
`Null` as its only implementation, and no MemPalace binding. The architecture
doc calls MemPalace brittle and lists memory as an open question; a contract
shaped by the component being replaced is the wrong contract.

Existing code to move here largely as-is: the tool loop and structured-output
path, the rate limiter, the sanitizer, the lease, the spec loader.

### WS-B — Persistence and job execution

`hunt_events` DDL, the ledger repository, the BullMQ worker running
re-entrant iteration steps (load ledger → advance one iteration → persist →
re-enqueue), and the watchdog that reclaims expired leases.

**Fold-equivalence gate.** Load each of the twelve existing `runs/*.jsonl`
through both the JSONL reader and the Postgres path; assert the projections
are deep-equal and every digest still replays exactly. The demo run already
replays 5/5, so there is a known-good baseline. This is the only real proof
that the storage change did not change semantics, and it is nearly free.

Helm reuse: `llm-worker-scaledobject.yaml` already autoscales a queue
consumer with KEDA. The BullMQ worker copies that shape rather than inventing
one.

### WS-C — Connectors

The query port and its adapters. Splunk and Elastic first — main has working
auth and REST code to port from, and both are already deployed against.
Per-dialect allow-lists, deny-by-default. Schema discovery as first-class
tools (list sources, describe one, sample from one), which the DuckDB
schema-in-tool-description approach cannot scale to.

DuckDB stays as the demo and test substrate.

MCP surface: one client registering MCP tools through the same `Tool` port,
allow-listed per role.

### WS-D — Hunt workflow

Port the existing hunt controller onto the kernel's seams: the decision
vocabulary and its validation, the digest builder, evidence strength,
termination, verdicts and the disconfirmation pass, entities, checkpoints,
enrichment, the report.

Mostly a re-homing rather than a rewrite — the risk is that it quietly keeps
its own budget and dispatch instead of using the seams. Reviewers should
check for that specifically.

### WS-E — Platform

`ai/` with its own `package.json`, mirroring `frontend/`. TypeScript tools
under `ai/tools/` so the Python `tools/` collision never arises.
`docker/Dockerfile.agent`, a Helm deployment mirroring the llm-worker's
shape, a Node job in CI alongside pytest. The rate table DDL, seeded from
`model_registry`'s existing table, and the deletion pass through the
redundant cost arithmetic.

### WS-F — API and frontend

The checkpoint mirror into `approval_actions`. A runs API. Later, for
`investigate`, the renderers that synthesise `plan.md`, `context.md` and
`review.md` from the ledger so the existing UI contract holds.

Sequenced last, but the mirror is small and should land with WS-D so
checkpoints are answerable from the product rather than only the CLI.

---

## 5. Cutover order

| Step | Replaces | Lines removed | Product risk |
|---|---|---|---|
| Kernel | — | 0 | none — additive |
| `hunt` workflow | — | 0 | none — new capability |
| `chat` caller | `claude_service` chat/chat_stream/AgentSDK, `openai_agent_service` | ~4,500 | high, mitigated by landing second |
| `compose` workflow | `workflows_service._run_phase_loop` | ~1,300 | medium |
| `investigate` workflow | `daemon/agent_runner`, orchestrator supervision | ~2,900 | highest — UI coupled |

`hunt` goes second because it is new: zero product risk while the kernel is
still moving. `chat` third because it is the largest single deletion with the
clearest contract, and a kernel serving both chat and hunt is *proven*
general rather than argued general. `investigate` last, so its UI problem is
solved against a mature kernel without schedule pressure.

`openai_agent_service` is pure deletion with no replacement — it exists only
because nothing was provider-neutral, and `provider.ts`/`wire.ts` removes the
need.

`daemon/orchestrator` is not a loop; it is a scheduler and supervisor. Its
triggering and watchdog responsibilities move to the BullMQ worker; its
supervision loop is deleted.

**The `investigate` risk, stated plainly.** Sixteen endpoints and six
frontend files hang off it, and `InvestigationDetail.tsx` reads workdir files
through `GET /investigations/{id}/files/{filename:path}`. A ledger does not
produce those files. The decision (D16) is to keep the API contract and
render those three documents from the ledger on read, so the frontend is
untouched at cutover. Those renderers are derived views and must never become
a second source of truth.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Harness/workflow boundary drawn from one workflow, then moves | Expected. `chat` lands second precisely to test it early, when moving it is still cheap |
| Tools reimplemented in TypeScript drift from Python equivalents | Delete the Python original at cutover rather than leaving both; drift needs two live copies |
| Both AI stacks coexist and the second cutover never happens | Cutover order is a commitment with line counts attached; track removed lines as a delivery metric |
| Postgres swap silently changes fold semantics | The fold-equivalence gate on twelve real ledgers (WS-B) |
| Caching layout regresses silently, costs rise unnoticed | Journal token counts including cache read/write; a cache-hit-rate assertion in CI |
| Per-dialect allow-lists have holes | Test against known-bad queries, not only known-good; treat a permitted unknown command as a bug |
| The mirror to `approval_actions` becomes a read path | Treat any read of `approval_actions` in the AI layer's control path as a review defect |
| Node runtime is new to the deployment | Mirror the llm-worker Helm shape rather than inventing one |

---

## 7. Open questions

Carried forward from the architecture document, unresolved and not blocking
Phase 0:

1. **Memory.** MemPalace is described as brittle. Recommendation is `Null`
   only until a replacement exists — needs confirmation.
2. **Dynamic tool availability and cost.** A wide tool catalogue costs tokens
   on every turn. Per-role allow-listing (D8) is a partial answer; whether
   tool sets should also vary within a run is open.
3. **Flexible schemas / flowprep**, and a histogram/time-series analysis
   agent — likely a future workflow, unscoped.
4. **The 13 SOC agents.** Whether they survive as-is, consolidate, or are
   re-cut per workflow is deferred to the `investigate` and `chat` cutovers,
   which is when it actually matters.
5. **Rate limiting** exists in three places (deeptempo-core, Bifrost, and the
   AI layer's own limiter). Recommendation: keep a client-side limiter for
   per-run fairness and fast local backpressure, and let Bifrost remain the
   global authority. Low stakes, easily revisited.

---

## 8. Definition of done for phase one

- Kernel and hunt workflow running in vigil, triggered through FastAPI,
  executing on the BullMQ worker.
- A hunt persists to `hunt_events`, resumes after a kill, and replays with
  every digest reproduced exactly.
- The twelve historical ledgers pass the fold-equivalence gate.
- A checkpoint is answerable from the product UI through the mirror, and its
  resolution is on the ledger.
- At least one SIEM adapter behind the query port with a tested allow-list.
- Token counts journaled; a measured cache hit rate on the frozen prefix.
- Nothing deleted from Python yet, and every subsequent cutover has a line
  count attached to it.
