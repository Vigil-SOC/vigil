# `core/llm` — the LLM layer

Everything Vigil does with a language model lives here. The layering below is
the point of the package: it is what tells you where a new file goes, and it is
enforced by `tests/unit/llm/test_boundary.py` and the `.importlinter` contracts.

## The harness boundary

The distinction that matters is **loop vs. call**. A *router* call is one
stateless completion. A *harness* runs many of them, executing tools between
turns. Conflating the two is what made `services/` unnavigable.

| Sub-package | Owns | Never does |
|---|---|---|
| `router/` | **One** stateless completion: provider selection, wire-format translation, pre-dispatch sanitization, budget VK resolution | Loop, execute tools, touch SOC domain models |
| `harness/` | The multi-turn agent loop: tool execution via MCP, approval gating, conversation state, streaming | Construct SDK clients or pick providers directly — it calls the router |
| `providers/` | SDK client construction, the model registry, live model discovery, local Ollama supervision | Know about agents or tools |
| `cost/` | Pricing math, pre-call estimation, virtual-key budget enforcement | Call an LLM |
| `bifrost/` | The only place that speaks Bifrost's admin and logging REST APIs | — |
| `gateway/` | The ARQ enqueue side in front of the router | Run the jobs it enqueues — that worker is `services/worker/` |

`security.py` (prompt-injection defenses) sits at the top level because both the
router and every harness need it.

## Rules

1. `router/` must not import `harness/`. The dependency runs one way.
2. Nothing under `core/llm/` imports `backend` or `daemon` at module scope.
   Lazy in-function imports of those are the sanctioned escape hatch. One
   module-scope import is grandfathered by name in the ratchet — `harness/
   claude.py` reading `backend.schemas.tool_schemas` — and belongs to #414.
3. `providers/registry.py` and `bifrost/admin.py` import each other lazily, by
   design. Do not hoist either import to module scope — it is a real cycle.
