# Versioning

Vigil uses semantic versioning. This file says what a caller may rely on across
releases and how that changes. How `VERSION` itself is bumped is on the
[releasing page](https://vigilsoc.org/docs/releasing/).

## What is frozen

- **The `/api/v1` HTTP contract.** Every path, operation and shape in
  [`core/api/v1/contract.snapshot.json`](core/api/v1/contract.snapshot.json).
  Routes marked `x-vigil-beta` are served under `/api/v1` but are versioned,
  not frozen: they are left out of the snapshot and may change until their
  shape settles.
- **The frozen tools on the `vigil` MCP server** at `/mcp`. The name and input
  schema of every tool in `FROZEN_TOOLS` (`tools/mcp/vigil.py`), as held by
  [`tools/mcp/frozen_tools.snapshot.json`](tools/mcp/frozen_tools.snapshot.json).
  Tools the server serves outside that set carry the `0.x` terms: they may
  change in any minor.

## What is not frozen

- Every `/api/*` route without a version: the console's own wiring. That
  includes analytics and reporting, `/api/config`, `/api/orchestrator`,
  `/api/llm/providers`, `/api/services` and `/api/ai`.
- `/internal/*`.
- The web console (`clients/web/`).
- The `WORKFLOW.md`, `SKILL.md` and `INTENT.md` file formats.

## How a frozen surface changes

- **Within a major, only additively.** New routes, new optional fields, new
  tools in the frozen set.
- **Removal or an incompatible shape change is a major.**
- **The legacy aliases.** Contract routers are also mounted at their old
  `/api/<resource>` paths through `RouterMeta.legacy_prefixes` (see
  [How dual-mounting works](core/api/v1/README.md#how-dual-mounting-works)).
  They are deprecated once the console has moved to `/api/v1`, and removed no
  earlier than 2.0.
- **Movement is one way.** A surface may move from unfrozen to frozen; nothing
  moves out of the open product.

## Changing the contract on purpose

Both snapshots are enforced by a drift test that fails on any difference. To
change one deliberately, run its generator, commit the snapshot diff, and say
so in the PR:

- `/api/v1`: see [Enforcement](core/api/v1/README.md#enforcement-the-contract-snapshot)
  (`scripts/generate_api_v1_contract.py`).
- MCP: `scripts/generate_mcp_frozen_tools.py`, checked by
  `tests/unit/api/test_mcp_frozen_tools.py`.
