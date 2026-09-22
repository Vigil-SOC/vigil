# The `/api/v1` contract surface

This package is the **versioned, frozen HTTP contract**. A route here is a
promise to external callers — contributors, the platform, other tools — that
its path and response shape stay stable across all 1.x releases. Everything
else (the console's own wiring) stays on the unversioned routers under
`services/api/routers/` and `core/<domain>/`.

## Why a folder, not a decorator

Membership is decided by **where the code lives**, so it is visible and
guardable. The contract snapshot test pins exactly this package's routes and
shapes; a decorator buried in a mixed file is not a set anything can snapshot or
review as a unit. Putting a route in the contract is then a deliberate act — you
move a file into this folder — not one stray line in a PR about something else,
and a reviewer sees what is promised by reading the tree.

## Collection paths carry no trailing slash

`/api/v1/findings`, not `/api/v1/findings/`. One spelling, and it is the one
this document gives, because the other spelling is not a route: it falls
through to the SPA fallback, which is GET-only, so a `POST` to it answers 405
and a `GET` answers the fallback. Register a collection as `@router.get("")`,
never `@router.get("/")` — the difference is one character and it moves the
frozen path. `tests/unit/api/test_api_v1_reachable.py` holds the line.

## How dual-mounting works

Each contract router is mounted twice: at its versioned path and at its old
path. One set of handlers, two addresses — no duplicated bodies.

    ROUTER_META = RouterMeta(
        prefix="/api/v1/findings",           # versioned home
        legacy_prefixes=("/api/findings",),  # old path keeps working
        ...
    )

Existing callers (the web console's ~314 calls) keep using `/api/findings`
untouched; new/external callers use `/api/v1/findings`. The legacy mount is
dropped in a later release once callers have moved.

Mechanism lives in `core/routing.py` (`legacy_prefixes`) and
`services/api/discovery.py` (mounts each router at `prefix` + every legacy
prefix; a router under `core/api/v1/` is namespaced `v1_<resource>` so it does
not collide with the console router it split from).

## Deciding internal vs external

A route is **external** (belongs here) if a caller that is not the Vigil
console would rely on it. Ask, in order:

1. Is it a durable record? (finding, case, verdict, approval) -> external
2. Would the platform or an outside script read/write it to do its job? -> external
3. Is it a UI button? (pause SLA, add watcher, post comment, toggle setting) -> internal
4. Is it plumbing or destructive? (config, `reload`, harness write-back, `DELETE /all`) -> internal

Rule 4 beats rule 1 when they disagree, which is why deleting a **case** is not
in the contract. A case is a durable record, so rule 1 says external; but
destroying one is an administrative act on an audit trail, and freezing its
semantics for a 1.x lifetime is a promise about what happens to evidence.
`DELETE /api/cases/{case_id}` therefore stays unversioned, deliberately, and so
does anything else that removes a record rather than changing it. Closing a case
is the external way to end one, and it is frozen.

Tie-breaker when fuzzy: **"If I freeze this shape for a year, will I regret
it?"** Confident it is stable -> freeze. Shape will churn as the feature matures
-> keep it unversioned, or mark it beta in the versioned surface.

Cheat: look at who calls it today. Only `clients/web/` -> probably internal.
The daemon, an integration, or nobody-yet-but-the-platform-will -> external.

## Two dual-mount shapes

**Default — legacy mount.** The v1 router lists its old path in
`legacy_prefixes` and serves both. Used when the console remainder has no route
that could collide with a v1 route across the two mounts. (findings, approvals.)

**Additive — when a `/{param}` route would shadow a retained literal.** A
parameterised route (`GET /{workflow_id}`) and a literal (`GET /custom`) are
only first-match-safe inside *one* router; splitting them across two routers
under the same prefix makes resolution depend on mount order, which
`test_no_cross_router_path_shadowing` forbids. So the v1 router is **not**
legacy-mounted — it serves only `/api/v1/<resource>` and is the source of
truth — and the console keeps its old routes as thin **delegators** that call
the v1 functions, staying in-router with the literal siblings. (workflows: the
catalog `GET /{workflow_id}` would otherwise shadow `GET /workflows/custom`.)

## The per-resource recipe

1. Split the router's routes into contract vs. console by the rule above.
2. Contract routes -> `core/api/v1/<resource>_router.py` with `prefix=/api/v1/<resource>`
   and `legacy_prefixes=("/api/<resource>",)`.
3. Non-contract routes stay in the console router at `/api/<resource>`.
4. Shared request/response schemas are defined in the v1 module; the console
   router imports them back (`services -> core`, the only allowed direction —
   `core -> services` is a layering violation).
5. Verify: discovery lists both `<resource>` and `v1_<resource>`; live OpenAPI
   shows the contract routes at `/api/v1/<resource>` and the full set still at
   `/api/<resource>`; `tests/unit/api/test_router_discovery.py` is green.

## Progress

| Resource   | Status        | In / total | Notes                                        |
|------------|---------------|------------|----------------------------------------------|
| findings   | done          | 5 / 8      | 3 console routes stay (bulk-enrich, enrich, wipe) |
| approvals  | done          | 5 / 5      | whole router is contract; moved wholesale    |
| cases      | done          | 16 / 40    | record + lifecycle (close/merge) + findings link + evidence + iocs. OUT (ruled): delete-all, delete-case, activities, resolution-steps, generate-report, chain-of-custody, sla ×4, comments ×4, watchers ×3, tasks ×3, relationships ×2, escalate ×2. (40 routes, not 39 — first sweep missed the nested chain-of-custody route.) |
| workflows  | done          | 2 / 16     | catalog only (list + get); runs are agent-runs, not workflow runs. Additive v1 + console delegates (see below) |
| runs (agent-runs) | done   | 4/3       | whole move of the 3 routes (start/get/directives) + built `GET /api/v1/agent-runs` (`list_runs`, reads workflow_runs filtered to source=agent). Frozen run surface = agent runs, not workflow runs or `/internal/runs` |
| analytics  | deferred      | —          | RULED: not frozen now (routes exist but shape will churn)     |
| cases/metrics | done       | 6 / 12     | all 12 under `/api/v1/cases/metrics`; 6 frozen, 6 marked `x-vigil-beta` (excluded from the contract snapshot). Frozen: by-priority, by-status, breached, mttr, mttd, summary |
| verdicts   | deferred      | build      | no routes exist yet                          |
| learning   | deferred      | build      | no routes exist yet                          |

Doc lists 7 frozen resources; we ship the 5 that exist and note verdicts +
learning as a known gap.

## Enforcement (the contract snapshot)

`core/api/v1/contract.snapshot.json` is the committed promise: the paths,
operations and referenced schema shapes of `/api/v1/**`, minus any operation
carrying `x-vigil-beta`. `scripts/generate_api_v1_contract.py` builds it from
the live app (deterministic: stable operation ids, transitive schema closure);
`tests/unit/api/test_api_v1_contract.py` rebuilds it and fails if it differs
from the committed file. A change to a frozen route or shape is therefore a red
build, not a silent edit — to change the contract on purpose, run the generator
and commit the diff. This is narrower than `schema.d.ts` (whole spec, must stay
free to move for the console) and stricter than `test_openapi_spec_stability.py`
(prefixes only).

To mark a versioned-but-not-frozen route, pass
`openapi_extra={"x-vigil-beta": True}` on its decorator; it stays under
`/api/v1` and out of the snapshot until its shape settles.
