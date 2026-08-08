# 1. The AI layer is TypeScript; the backend stays Python

Date: 2026-08-07

## Status

Accepted

## Context

Vigil's main branch is ~191k lines of Python: FastAPI backend, an ARQ/Redis
daemon, Postgres, Helm charts, and a React frontend. Its AI surface is
~22,130 lines spread over five agent loops, three tool dispatchers, and four
transport modules.

The `threat-hunt` branch is 53 files and zero Python. It is not a feature
branch on main — `git diff --stat main threat-hunt` is 755 files changed,
15,391 insertions, 191,755 deletions. It shares git history with main and
nothing else.

`docs/plan.md:493` scoped the replacement as `ai/loop.py`, and `:331` placed
the domain logic in a Python `core/hunting/` running as re-entrant ARQ steps.
That scoping also assumed a `core/llm/` neutral loop was already merged
(`:329`); it is not — `core/` on main is eight files of config, exceptions,
rate limiting, secrets and telemetry.

So a decision could not be deferred: either the TypeScript work is ported to
Python, or the Python AI surface is replaced by TypeScript.

## Decision

The entire AI layer is TypeScript, in `ai/` with its own `package.json`. The
backend — auth, cases, findings, the UI-facing API, federation ingest, the
pollers — stays Python.

The AI layer owns its own Postgres tables and is the single writer to them.
It is not a stateless service that asks Python for permission to persist.

## Consequences

The repo is already polyglot (`frontend/` is TypeScript), so this moves an
existing boundary rather than introducing one. It does introduce a second
runtime in the deployment: a Node worker image, its own dependency tree, and
a Node job in CI.

ARQ is a Python library, so the AI layer cannot consume its queue. Job
distribution uses BullMQ on the same Redis instance, with FastAPI enqueuing
plain JSON. `services/llm_gateway.py`'s `arq:llm` queue disappears entirely
rather than needing a bridge, because the TypeScript loop replaces both ends
of it.

There is no service-to-service auth on main — `backend/middleware/auth.py` is
user-JWT only and verifies a session fingerprint a machine client cannot
satisfy. Rather than build one, vigil's tools are reimplemented in TypeScript
against Postgres directly. This is a real cost: business logic that exists
once in Python gets a second implementation, and the two can drift.

The load-bearing technical argument is discriminated unions with
exhaustiveness checking over a closed decision vocabulary: adding a verb
breaks compilation at every site that does not handle it. Python's `Literal`
plus mypy approximates this but is not enforced in CI on main. Arguments from
error handling and scalability were considered and are not load-bearing —
Python has equivalent exception handling, and the loop is I/O-bound on model
calls, so Node's single thread is not a constraint.

## Alternatives considered

**Port the TypeScript to Python.** Faithful to `docs/plan.md`, one language,
one deploy, and direct access to the case, approval and cost machinery that
JVL named as vigil's value (`plan.md:605`). Rejected in favour of the type
guarantees above.

**Keep TypeScript as a sidecar the Python loop calls.** Preserves both trees.
Rejected: it makes the loop count worse rather than better, and splits state
between a JSONL ledger and Postgres.
