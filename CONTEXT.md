# Vigil SOC

Vigil is an AI-native Security Operations Center: specialized AI agents perform
triage, investigation, threat hunting, and response across many security
integrations. This glossary fixes the language used across the codebase and
docs. It is a glossary, not a spec — it defines what terms *mean*, not how any
component is built.

## Language — LLM plumbing

**Provider**:
An LLM backend Vigil can send a call to — currently Anthropic, OpenAI, or a
local Ollama instance. A provider has a type and one or more usable models.
_Avoid_: vendor, backend, engine (see "Engine").

**Active provider**:
The provider a given call actually runs on — the configured default of any type,
not necessarily Anthropic. Distinct from "the Anthropic provider," which is one
specific provider that may or may not be active.
_Avoid_: current provider, selected provider.

**Model**:
A specific named LLM offered by a provider (e.g. `claude-sonnet-4-6`,
`qwen2.5:14b`). A model belongs to exactly one provider type; a Claude model
must never be routed to a non-Anthropic provider.

**LLM entry point**:
The single seam every LLM call passes through. All model calls go through it so
provider selection, tool execution, streaming, and persistence live in one place.
_Avoid_: LLM client, chat service (those name implementations, not the role).

**Agentic tool loop**:
The multi-turn cycle in which a model requests a tool, Vigil executes the tool,
feeds the result back, and repeats until the model produces a final answer.
Owned by the LLM entry point, not by individual callers.
_Avoid_: agent loop, tool loop (acceptable shorthands), ReAct loop.

**Transport**:
The mechanism that carries one request to one provider and returns its response.
A single turn within the agentic tool loop. Provider-specific.
_Avoid_: dispatch call (reserve "dispatch" for the act, not the mechanism).

**Engine**:
A provider-specific implementation of a turn — e.g. the Anthropic path or the
OpenAI-compatible path. Engines are internal to the LLM entry point; callers
never talk to an engine directly.

**Gateway**:
The queue front for LLM work: it accepts a request, enqueues it for a background
worker, and is how asynchronous/long-running calls are submitted. Distinct from
the entry point, which performs the call.
_Avoid_: queue service, job service.

## Flagged ambiguities

**"Claude"** is overloaded: it names the vendor (Anthropic), the SDK, and — in
older code — the LLM entry point itself (`ClaudeService`). Use **Anthropic** for
the provider, **Claude Agent SDK** for Anthropic's agent framework, and reserve
**LLM entry point** for the routing seam regardless of provider.

## Example dialogue

> **PM:** If someone's on Ollama with no Anthropic key, does chat still work?
> **Dev:** Yes. The active provider is Ollama, so the entry point picks the
> Ollama engine as transport. It never touches the Anthropic engine.
> **PM:** And if a stale request asks for a Claude model?
> **Dev:** The entry point sees a Claude model on a non-Anthropic active
> provider and swaps it for that provider's own model before dispatch.
> **PM:** Where does the agentic tool loop run?
> **Dev:** In the entry point. The caller just asks for a chat; the loop,
> tool execution, and persistence all happen behind that one seam.
