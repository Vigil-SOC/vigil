# The `/api/v1` contract surface

This package is the **versioned, frozen HTTP contract**. A route here is a
promise to external callers — contributors, the platform, other tools — that
its path and response shape stay stable across all 1.x releases. Everything
else (the console's own wiring) stays on the unversioned routers under
`services/api/routers/` and `core/<domain>/`.

Related issue: #860.

## Why a folder, not a decorator

Membership is decided by **where the code lives**, so it can be enforced. A
`CODEOWNERS` rule on `core/api/v1/` forces a maintainer review on every
contract change; nothing can require review based on a decorator buried in a
mixed file. Putting a route in the contract is then a deliberate act — you move
a file into this folder — not one stray line in a PR about something else.

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
| runs       | todo          | agent-runs | RULED: runs = agent runs (`/api/agent-runs`), not workflow runs; also build the missing `list_runs` |
| analytics  | deferred      | —          | RULED: not frozen now (routes exist but shape will churn)     |
| cases/metrics | blocked    | 6 / 12     | ruling: under cases; 6 freeze, 6 beta        |
| verdicts   | deferred      | build      | no routes exist yet                          |
| learning   | deferred      | build      | no routes exist yet                          |

Doc lists 7 frozen resources; we ship the 5 that exist and note verdicts +
learning as a known gap.
