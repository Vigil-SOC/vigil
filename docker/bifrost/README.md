# Bifrost Gateway (Sidecar)

[Bifrost](https://github.com/maximhq/bifrost) is a compiled Go binary that exposes a unified OpenAI-format API and translates requests to Anthropic, OpenAI, Ollama, and other providers. Vigil uses it so we don't need to write and maintain a per-provider abstraction in Python for every supported backend.

## How Vigil uses Bifrost

- **Non-Anthropic traffic** (Ollama, OpenAI) is sent as OpenAI-format chat completions to `BIFROST_URL` (default `http://bifrost:8080`). Bifrost handles the provider-specific translation.
- **Anthropic + extended thinking** bypasses Bifrost and goes directly through the `anthropic` Python SDK in `services/claude_service.py`. This is because extended thinking and Anthropic's native prompt caching don't round-trip cleanly through Bifrost's OpenAI-format surface today.
- The routing decision lives in `services/llm_router.py` (`_select_path`).

## Starting Bifrost

Bifrost runs under the `bifrost` docker-compose profile so a plain `docker compose up` doesn't pull the image:

```bash
docker compose --profile bifrost up postgres redis bifrost backend llm-worker
```

Health check: `curl http://localhost:8080/health`.

## Configuration

`docker/bifrost/config.json` declares the providers and the models Bifrost will expose. API keys are **not** written into the config file — they are injected as environment variables at container start time (`env.ANTHROPIC_API_KEY`, `env.OPENAI_API_KEY`, `env.OLLAMA_URL`). Vigil's backend reads per-provider keys from its own `secrets_manager`; what's in Bifrost's env are only the fallback/default keys used when a provider row in `llm_provider_configs` doesn't override them.

To add another provider or model, edit `config.json` and restart the `bifrost` service. See the upstream Bifrost documentation at https://github.com/maximhq/bifrost for the full config schema.

## Tool-use support matrix

| Provider | Basic chat | Tool calling | Streaming | Thinking / caching |
|---|---|---|---|---|
| Anthropic via Bifrost | ✅ | ⚠️ limited | ✅ | ❌ (use direct SDK) |
| Anthropic direct SDK | ✅ | ✅ | ✅ | ✅ |
| OpenAI via Bifrost | ✅ | ✅ | ✅ | n/a |
| Ollama via Bifrost | ✅ | ⚠️ model-dependent (Llama 3.1+, Mistral) | ✅ | n/a |

For anything that needs Anthropic-native features, keep `provider_id=anthropic-default` and `enable_thinking=true` — the router will pick the direct SDK path.
