# 2. A domain-free harness with deterministic workflows attached

Date: 2026-08-07

## Status

Accepted

## Context

The rearchitecture is meant to produce one loop serving many agent types.
The obvious reading — one generic ReAct loop, with every agent type expressed
purely as prompts, tools and yaml — was considered and rejected.

The hunt implementation is not generalisable as configuration. Measured
across its 25 modules, `hypothes*` appears 399 times in 14 files, `checkpoint`
262, `verdict` 148, `proven` 65. The spec loader deliberately refuses to let
a configuration file widen the decision vocabulary; it may only narrow it.
So a second agent type cannot be a second yaml over the same controller: a
triage agent has no hypothesis to validate, and a responder needs a
containment verb that does not exist.

The architecture document already draws the correct split, in two separate
diagrams that are easy to read as one. Its `ai/loop.py` flowchart is
`provider.stream → tool calls? → approval required? → dispatch → security
wrap → write state → terminal status? → budget ok?` — a streaming tool loop
with statuses `running | waiting_approval | completed | failed`. Its
hypothesis diagram is a different thing entirely, and sits under the heading
"Nuanced deterministic workflow attached to general harness".

## Decision

Two layers, named distinctly.

The **harness** is domain-free: the tool loop, provider surfaces, context
assembly, tool registry and dispatch, injection scanning, budget, state and
memory. It knows about turns, tools, spend and approval. It knows nothing
about what is being investigated.

A **workflow** is a deterministic domain state machine attached to the
harness. It owns a decision vocabulary, what each verb mutates, and when a
run may end. Hunting is one workflow. Triage, response and phase composition
are others.

A new agent type is a new workflow module plus its configuration — not
configuration alone.

## Consequences

`ai/loop.py ~400` in the architecture document describes the harness, not the
hunt controller. The apparent 4.5x overrun against the hunt controller's 1,803
lines is a category error; they are different layers. This must be stated
plainly wherever the estimate is quoted, or someone will try to fit a domain
state machine into 400 lines.

The proposal's "five loops to one" is similarly ambiguous, because the five
it counts mix both kinds. The target is **one harness loop and one workflow
loop per agent type**.

Controller-side gating survives generalisation: the citation requirement, the
refusal to conclude while a hypothesis is active, corroboration counted across
declared source systems, and the independent disconfirmation pass all live in
the workflow layer, where they are code rather than prompt. This is what
makes the output trustworthy in the sense the product requires, and it is
exactly what a purely generic loop would have discarded.

The cost is one module per agent type rather than one file. The boundary
between harness and workflow is drawn from a single real case until the
second workflow lands, so it should be expected to move once.

## Alternatives considered

**Generic ReAct loop; domain in prompts and tools only.** Every agent type
becomes pure yaml. Rejected: the model's confidence would gate behaviour
again, which is the failure the design exists to prevent.

**Vocabulary-as-data: verbs and their effects declared in yaml.** Rejected:
the controller's value is what it refuses, and expressing "refuse unless
corroborated across two declared source systems" as configuration means
inventing a DSL harder than the TypeScript it replaces.
