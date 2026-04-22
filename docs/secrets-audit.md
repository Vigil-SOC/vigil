# Secrets Audit (GH #84 PR-F)

Snapshot of how Vigil handles credentials at rest today, and the gaps that
a future migration should close. Generated alongside the PR-F Settings-UI
surface for runtime cost toggles — a companion to that work, not a
migration itself.

## How Vigil stores secrets today

Three storage layers, in priority order (see
[backend/secrets_manager.py](../backend/secrets_manager.py)):

1. **Environment variables** — process env (`os.environ`). Always
   available; cannot be rotated at runtime.
2. **`~/.deeptempo/.env`** — per-user dotenv file, chmod `0o600`. Default
   write target for `set_secret()`. Survives restart but is host-local.
3. **OS keyring** (macOS Keychain, Linux Secret Service) — opt-in via
   `ENABLE_KEYRING=true`. Best for desktop/dev; not used by the server
   deployment path.

`get_secret(key)` checks them in that order; `set_secret(key, value)`
writes to whichever backend is configured via `SECRETS_BACKEND`.

## Where each credential actually reads from

### ✅ Already routed through `secrets_manager`

| Secret | Read sites | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` | [services/claude_service.py:256](../services/claude_service.py), [backend/services/ai_insights_service.py:29](../backend/services/ai_insights_service.py), [services/llm_router.py:214](../services/llm_router.py) | Default Anthropic provider. `get_secret()` layers env → dotenv → keyring. |
| Per-provider LLM keys (OpenAI, Ollama, custom Anthropic) | [services/llm_router.py:209](../services/llm_router.py), [services/llm_worker.py:277](../services/llm_worker.py) | GH #88 design: `LLMProviderConfig.api_key_ref` in DB points at a `secrets_manager` key (e.g. `llm_provider_anthropic-team_api_key`). **Keys themselves never land in the DB.** Settings UI → `backend/api/llm_providers.py` writes via `set_secret()`. |
| `GITHUB_TOKEN` | [backend/main.py:182](../backend/main.py) | MCP server setup. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | [services/database_data_service.py:590-591](../services/database_data_service.py) | S3 export path. |
| S3 per-config credentials | [backend/api/config.py:225-350](../backend/api/config.py) | Saved via `set_secret()` next to the non-secret bucket/region config. |

### ❌ Still read directly from `os.getenv` — migration candidates

| Secret | Read site | Risk | Recommended target |
|---|---|---|---|
| `SPLUNK_URL` / `SPLUNK_USERNAME` / `SPLUNK_PASSWORD` | [tools/_legacy/splunk.py:35-41](../tools/_legacy/splunk.py) | Plaintext password in process env and `.env`. | `secrets_manager` + Settings-UI Splunk panel. Note: the `_legacy/` prefix suggests this tool is being phased out; may be simpler to finish the replacement than to migrate. |
| `POSTGRES_PASSWORD` | [database/connection.py:39](../database/connection.py) | Infrastructure credential; typically injected by docker-compose. Medium risk — host-level anyway. | Acceptable to keep in env if production uses a real secrets manager (Vault / AWS Secrets Manager) to populate the env at pod start. |

### ⚠️ Declared in `env.example` but no active readers found

These are either (a) unused templates, (b) consumed only by integrations
configured per-provider via the DB (`LLMProviderConfig`,
`IntegrationConfig`), or (c) referenced only by Bifrost's own
`config.json`:

- `OPENAI_API_KEY`, `OLLAMA_URL` — consumed by Bifrost container env
  (see [docker/bifrost/config.json](../docker/bifrost/config.json)); also
  usable as *seeds* when the UI adds a provider for the first time.
- `VIRUSTOTAL_API_KEY`, `SHODAN_API_KEY`, `ALIENVAULT_OTX_API_KEY` —
  consumed by MCP tool containers, not read from Python.
- `CROWDSTRIKE_CLIENT_*`, `SLACK_*`, `PAGERDUTY_*`, `JIRA_*`,
  `ELASTIC*` — configured per-integration via
  `IntegrationConfig` (DB) once the Settings-UI wizard runs; the env
  vars are only fallbacks for first boot.

**This is not necessarily a problem.** The pattern is deliberate: the UI
writes integration secrets to `secrets_manager` at config time and the
DB holds the reference. What looks like "no reads" in Python is "reads
happen through a per-integration `get_secret(integration_id + '_*')`
lookup or via the MCP tool container's env."

## Migration priorities

If we decide to harden further, in descending order of impact:

1. **Splunk legacy tool** — still reads password via `os.getenv` in
   [tools/_legacy/splunk.py](../tools/_legacy/splunk.py). Either delete
   the legacy path (if the replacement is live) or route through
   `get_secret("SPLUNK_PASSWORD")`.
2. **Bifrost container env** — OpenAI / Anthropic / Ollama keys passed
   to Bifrost via env vars live in cleartext in `docker-compose.yml`'s
   env block at runtime. For production, inject these from a host-level
   secrets manager (Vault agent, AWS Secrets Manager, K8s secret) rather
   than `.env`. *Not a code change — deployment doc change.*
3. **Settings-UI surfaces for integration secrets** — some integrations
   (Slack, PagerDuty, Jira) don't yet have a Settings-UI surface that
   writes to `secrets_manager`. Adding those panels brings them in line
   with the S3 / LLM-provider pattern.
4. **Centralize reads** — audit and remove every remaining
   `os.getenv("*_API_KEY")` or `os.getenv("*_PASSWORD")` from the Python
   codebase; the single source of truth for credential reads should be
   `secrets_manager.get_secret(...)`. A CI grep guard would prevent
   regressions.

## Operational toggles (PR-F subject) — these are NOT secrets

The four values exposed in the new `AIOperationsTab`
(`prompt_cache_enabled`, `history_window`, `tool_response_budget_default`,
`thinking_budget`) are **not secrets** — they're cost/perf knobs. They
correctly live in `system_config` (DB, plaintext) with env-var
fallbacks, and the Settings UI writes them via
`POST /config/ai-operations`. No secrets-manager involvement required.
