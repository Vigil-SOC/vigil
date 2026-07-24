# LLMRouter is the sole entry point for LLM calls

- **Status:** accepted
- **Date:** 2026-07-23
- **Issue:** [#413](https://github.com/Vigil-SOC/vigil/issues/413)

## Context

Vigil must run its agents on any configured provider (Anthropic, OpenAI, or a
local Ollama model), not just Anthropic. Today fourteen non-test modules import
`services.claude_service.ClaudeService` directly, and most call
`ClaudeService.chat(use_backend_tools=True)` — a single call that couples LLM
dispatch with the multi-turn agentic tool loop and MCP tool loading. Because
`ClaudeService` is the Anthropic SDK path, every one of those call sites is
implicitly Anthropic-only, which is what blocks non-Anthropic providers. There
is no enforced boundary, so new code keeps reaching for `ClaudeService`.

## Decision

`LLMRouter` (`services/llm_router.py`) becomes the **single entry point for all
LLM calls**. It absorbs the multi-turn agentic tool loop, streaming, interaction
persistence, and a passthrough for the Anthropic-only Claude Agent SDK
(`run_agent_task`). `ClaudeService` and `OpenAIAgentService` become internal
engines that only `LLMRouter` may import. The boundary is enforced by an
import-linter contract in CI that forbids `from services.claude_service import`
anywhere except `LLMRouter` and tests.

## Considered options

- **A — A higher-level facade seam** (extend `LLMGateway` or a new `services/llm.py`)
  as the entry point, with `LLMRouter` kept as a pure transport. Rejected: does
  not match the issue's framing and would leave two "front doors" (gateway +
  router).
- **B — `LLMRouter` literally becomes the entry point (chosen).** Matches the
  issue's wording and gives one class to reason about.
- **C — Keep `ClaudeService` intact but hide it behind `LLMRouter`.** Rejected:
  the dispatch/tool-loop coupling inside `ClaudeService` would remain — the
  contract would pass without the underlying cleanup.

## Consequences

- `LLMRouter` grows to own two loops (its provider-agnostic tool loop plus the
  Anthropic Agent SDK passthrough). This concentrates responsibility and carries
  a "god-object" risk; PR3 (the loop relocation) warrants a dedicated
  fresh-eyes engineering review.
- All LLM-dispatch callers must be redirected off `ClaudeService`; the daemon's
  tool execution and the worker's persistence move behind the router.
- Once enforced, any new module that imports `ClaudeService` fails CI, keeping
  the boundary from eroding.
