# Context

Glossary for the vigil AI loop rearchitecture. Terms only — no implementation
detail, no decisions. Decisions belong in `docs/adr/`.

## Language boundary

**AI layer** — everything that reasons with a model: the loop, the ledger,
agents, tools, prompts, specs. Written in TypeScript. Owns its own persistence.

**Backend** — auth, cases, findings, users, the UI-facing API, federation
ingest, and the pollers. Stays Python for now. Not a synonym for "the server":
the AI layer is server-side too.

## Loops

Two distinct nested loops. Saying "the loop" without qualification is
ambiguous and has already caused a miscount (five loops named in the rearch
proposal are a mix of both kinds).

**Decision loop** — the outer loop. One turn per iteration: read a digest of
the ledger, emit exactly one decision from a closed vocabulary, apply it,
append to the ledger. Costs one iteration against the budget. Terminates on a
deterministic predicate the model does not control.

**Tool loop** — the inner loop, inside a single dispatched agent. Model calls
a tool, reads the result, calls another, until it answers or hits a turn cap.
Costs no iterations; the whole loop is one dispatch. Nothing it does reaches
durable state except the evidence it returns.

**Harness** — the general, domain-free machinery: the tool loop, provider
surfaces, budget, state store, tool dispatch, context assembly, injection
scanning. Knows about turns, tools, spend and approval; knows nothing about
what is being investigated.

**Workflow** — a deterministic domain state machine attached to the harness.
Owns a vocabulary, what each verb mutates, and when a run may end. The
hypothesis loop is one workflow; triage and response would be others. Where
"is this trustworthy" is decided, which is why it is code and not prompt.

## Agents

**Lead** — the role that runs the decision loop. Reads the digest, never
queries telemetry. Exactly one per run.

**Worker** — a specialist the lead dispatches. Runs a tool loop, appends
findings, and is the only role with telemetry access. Cannot mutate state.

**Critic** — an independent role invoked by a specific verb, not on a
schedule. Argues against a conclusion rather than producing one. Reads raw
payloads, never the lead's account of them.

Workers are **leaves**: a worker never runs a decision loop and never owns a
ledger. Topology is therefore one axis — how many workers run at once. "Serial"
means a fan-out width of one; "swarm" means a wide fan-out. Neither is a
different control flow, and neither is a second lead. Work that genuinely needs
its own vocabulary and its own termination is a **separate run** linked to the
one that spawned it, not a nested loop.

**Controller** — deterministic code, not a role and not a model. Owns every
state mutation, applies decisions serially, and refuses ones that fail its
checks. Where trust lives: a decision the model emits is a request.

## State

**Ledger** — the append-only event log. The only durable state. Everything
else (projection, report, replay) is derived by folding it and is therefore
never separately persisted.

**Projection** — the current state, computed by folding the ledger. Not
stored. Two folds of the same ledger are identical by construction.

**Durable vs transient** — durable is what was appended or committed:
decisions, evidence, state transitions. Transient is tool-loop scratch and
streaming tokens. If it was not appended, it did not happen for replay.

**Replay** — refolding a ledger prefix to rebuild the state behind a past
decision, and checking it matches what was recorded at the time. A property
that can be verified, not a claim.

## Configuration layers

Three disjoint layers. A key in the wrong layer is a load error, not a
warning.

**Arch** — the shape of the run: which roles exist, what each may do, the
decision vocabulary, digest policy. Domain-shaped, reusable across scenarios.
May narrow the vocabulary the controller implements; may never widen it.

**Playbook** — the scenario as data: what is being investigated, scope, data
domains, starting premises. Carries no model or infrastructure settings.

**Config** — deployment: model, rates, budgets, tools, concurrency, and which
checkpoints ask a human. Carries no scenario content.

## Tools

**Tool** — a capability a role may invoke, declared in config and bound to a
role in the arch. The unit a worker's tool loop calls. Every tool is reached
through one port regardless of what backs it, so a role's tool list says
nothing about transport.

**MCP surface** — an MCP server exposed as tools through that same port. Its
tools are registered individually and allow-listed per role: a role receives
the tools it was granted, never a server's full catalogue. Transport is an
implementation detail of the surface, not a second kind of tool.

**Query port** — the narrow interface every telemetry backend implements:
one query in a native dialect, bounded rows and time, rows out. Read-only
enforcement, row capping and timeout live inside it, so no caller can opt out
of them. A backend that cannot enforce those does not get a query port.

**Visibility gap** — a question the run wanted answered and could not get. A
real finding about the environment, recorded as such. Distinct from a tool
failure and from a question the query surface merely could not express — the
latter is a defect, and must never be recorded as a gap.

## Judgement

**Stated confidence** — the model's self-report. Recorded for calibration,
gates nothing.

**Evidence strength** — controller-computed from the ledger: corroborating
source systems, contradicting records, open gaps, whether support rests only
on attacker-writable fields, critic survival. Everything that gates behaviour
gates on this.

**Source system** — the telemetry domain a finding came from, not the agent
that found it. The unit corroboration is counted over, which is why it is
closed to a declared list.

**Checkpoint** — a point where the run parks and waits for a human. Spends
neither iterations nor money while parked. Distinct from a budget park, which
asks whether to extend rather than what to do.
