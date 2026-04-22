# Bifrost Gateway (Required)

[Bifrost](https://github.com/maximhq/bifrost) is a compiled Go binary that exposes a unified LLM gateway — OpenAI-format requests on `/v1` plus provider-native passthroughs (e.g. `/anthropic`). Vigil routes **all** LLM traffic through Bifrost so caching, cost tracking, and budget enforcement live in one place.

## How Vigil uses Bifrost

As of GH #84 PR-B, there is a single routing path:

- **Anthropic traffic** (including extended-thinking calls) hits Bifrost's Anthropic-compatible passthrough at `{BIFROST_URL}/anthropic` using the regular Anthropic SDK with a swapped `base_url`. This preserves extended thinking, `cache_control` blocks, and cache-token usage counters.
- **OpenAI / Ollama / other providers** hit Bifrost's `/v1` OpenAI-format surface.

The single source of truth for Anthropic client construction is `services/llm_clients.py` — every `Anthropic(...)` / `AsyncAnthropic(...)` call site in the repo goes through `create_anthropic_client` / `create_async_anthropic_client`, which point at Bifrost. The lone exception is `backend/api/llm_providers.py`, which validates user-supplied API keys against the upstream provider and therefore deliberately bypasses the gateway.

The routing decision lives in `services/llm_router.py`. There is no direct-SDK bypass — if Bifrost is unhealthy, LLM traffic fails loudly rather than silently taking a second path.

## Pre-flight capability probe (merge blocker)

Before shipping any change that modifies how Vigil talks to Bifrost, run:

```bash
BIFROST_URL=http://localhost:8080 \
ANTHROPIC_API_KEY=sk-ant-... \
python scripts/bifrost_capability_probe.py
```

This verifies Bifrost passes through:

- basic round-trip
- the `thinking` parameter (extended thinking)
- `cache_control: {"type": "ephemeral"}` on system blocks → cache creation
- second-call cache hits surfaced as `cache_read_input_tokens` in the usage object

**If any probe fails, do not ship.** File an upstream issue on https://github.com/maximhq/bifrost and hold the change until it's fixed. Per project policy (see memory: "Single LLM routing path"), we never reintroduce a direct-SDK bypass to work around a gateway limitation.

## Starting Bifrost

Bifrost is always-on — `docker compose up` brings it up alongside the rest of the stack:

```bash
docker compose up postgres redis bifrost backend llm-worker
```

Health check: `curl http://localhost:8080/health`.

## Configuration

`docker/bifrost/config.json` declares the providers, the models Bifrost will expose, and the cache backend. API keys are **not** written into the config file — they are injected as environment variables at container start time (`env.ANTHROPIC_API_KEY`, `env.OPENAI_API_KEY`, `env.OLLAMA_URL`). Vigil's backend reads per-provider keys from its own `secrets_manager`; what's in Bifrost's env are the fallback/default keys used when a provider row in `llm_provider_configs` doesn't override them.

### Caching — two layers

Vigil benefits from two independent caching layers. They're **complementary**, not redundant:

| Layer | What it caches | Hit rate in practice | Savings | Run by |
|---|---|---|---|---|
| Anthropic native prompt caching (GH #84 PR-C) | Request prefix (system prompt + tool schemas) | Most calls within a session | ~90% on cached input tokens | Anthropic |
| Bifrost semantic cache (optional) | Full responses, keyed on embedding similarity | Only semantically similar retries | 100% on hit | Bifrost + Redis vector store |

**Anthropic prefix caching** is turned on by default (`ANTHROPIC_PROMPT_CACHE_ENABLED=true`, kill-switch in Settings → AI Config → AI Operations). The `cache_control` markers are added in `services/claude_service.py:_apply_prompt_cache_controls` and preserved by Bifrost's `/anthropic` passthrough — verified by `scripts/bifrost_capability_probe.py`.

**Bifrost's semantic cache** is opt-in and configured through the Bifrost UI at http://localhost:8080 (not via `docker/bifrost/config.json` — v1.4.23 rejects a top-level `cache` block). Enabling it requires:

1. A vector store (Redis recommended — reuse the existing Redis, a separate DB number)
2. An embedding provider (OpenAI `text-embedding-3-small` or a local Ollama model)
3. Enabling the `semantic_cache` plugin in the UI

Run `python scripts/bifrost_cache_status.py` to check whether it's live. Vigil's unified routing does **not** depend on this layer — it's optional cost gravy on top of Anthropic's native prefix caching.

## Production deployment: keep API keys out of `.env`

Local `docker compose up` injects `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
into Bifrost via the `environment:` block, which resolves from
`.env` in the compose project directory. **That's fine for development;
it's not fine for production** — the `.env` file lives on the host, is
read by every process in the compose network, and doesn't rotate.

For production deployments, inject Bifrost's provider keys from a real
secrets manager at container start. A few patterns:

### HashiCorp Vault (agent-injector or init container)

```yaml
services:
  bifrost:
    image: maximhq/bifrost:latest
    # Vault agent writes rendered env file to a tmpfs shared volume;
    # the container sources it before the main process starts.
    command: ["sh", "-c", ". /vault/secrets/bifrost.env && exec /app/main"]
    volumes:
      - vault-secrets:/vault/secrets:ro
```

### AWS Secrets Manager (ECS task definition)

Use ECS `secrets` (not `environment`) so the agent resolves the ARN at
task launch and injects the value as an env var that never hits the
task-definition JSON:

```json
"containerDefinitions": [
  {
    "name": "bifrost",
    "secrets": [
      {"name": "ANTHROPIC_API_KEY", "valueFrom": "arn:aws:secretsmanager:...:vigil/anthropic-api-key"},
      {"name": "OPENAI_API_KEY",    "valueFrom": "arn:aws:secretsmanager:...:vigil/openai-api-key"}
    ]
  }
]
```

### Kubernetes (Secret → envFrom)

```yaml
envFrom:
  - secretRef:
      name: vigil-llm-provider-keys
```

With the `Secret` populated via Sealed Secrets, External Secrets
Operator, or whatever mechanism you already use for other app secrets —
Bifrost doesn't care, it just wants the env var present at startup.

### What stays in `.env`

Non-secret Bifrost config (URL, log level, etc.) is fine to keep in
plaintext. The distinction is: if leaking the value to a host-mounted
`.env` file is acceptable, leave it in env; otherwise, route it
through your secrets manager of choice.

## Tool-use support matrix

| Provider | Basic chat | Tool calling | Streaming | Thinking / caching |
|---|---|---|---|---|
| Anthropic via Bifrost `/anthropic` | ✅ | ✅ | ✅ | ✅ (verified by capability probe) |
| OpenAI via Bifrost `/v1` | ✅ | ✅ | ✅ | n/a |
| Ollama via Bifrost `/v1` | ✅ | ⚠️ model-dependent (Llama 3.1+, Mistral) | ✅ | n/a |
