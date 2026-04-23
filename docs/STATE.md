# Where Vigil stores state

This project grew with state scattered across several locations. This
doc is the canonical map: where each kind of thing lives, which store
is authoritative, and what's deprecated.

## Secrets

**Authoritative store: `~/.vigil/secrets.enc`** (Fernet-encrypted JSON,
chmod 600). The symmetric key lives at `~/.vigil/master.key` (chmod 600,
auto-generated on first write). Both files sit outside the repo, so
`.env` rewrites, `setup_dev.sh`, resetting the project dir, or git
checkouts don't nuke stored credentials.

**Backend priority** (read order in `SecretsManager.read_backends`):

1. `EncryptedFileBackend` — `~/.vigil/secrets.enc` (preferred)
2. `EnvironmentBackend` — `os.environ`
3. `DotEnvBackend` — `~/.deeptempo/.env` (**legacy, prefer migrate**)
4. `KeyringBackend` — OS keychain, only when `ENABLE_KEYRING=true`

**Default write backend:** `encrypted` (override with
`SECRETS_BACKEND=env|dotenv|keyring`).

**Editing a secret.** Use the LLM Providers UI (Settings → AI Config →
Providers) or the `set_secret()` helper from `backend.secrets_manager`.
Never write secrets into `.env` by hand; that file is for non-secret
bootstrap flags only (see below).

**Migrating legacy secrets.** Run once:

    python scripts/migrate_secrets.py          # copy → encrypted store
    python scripts/migrate_secrets.py --purge  # also strip from ~/.deeptempo/.env

**Backup.** Back up `~/.vigil/` as a unit — both files are needed.
Losing `master.key` means losing every secret in `secrets.enc`; there
is no recovery.

## Non-secret runtime config

**Authoritative store: Postgres `system_config` table.** Runtime-editable
settings (orchestrator tunables, AI operations, general/S3/Darktrace
settings, feature flags, etc.) live here with a full audit trail in
`config_audit_log`. Access via `backend.api.config` and the Settings UI.

New runtime config should go here — not into `.env`.

## `.env` (repo root)

**Purpose: bootstrap only.** Values needed to start the backend before
the DB is reachable. Nothing sensitive should live here.

Safe to put in `.env`:
- `DATABASE_URL`, `REDIS_URL`, `BIFROST_URL`
- `BIND_HOST`, port numbers
- `DEV_MODE`, `SECRETS_BACKEND`, `ENABLE_KEYRING`
- `SENTRY_DSN` (non-secret in practice)
- `LOG_LEVEL`, polling intervals, etc.

**Do not put in `.env`:** API keys, tokens, passwords. Store via the UI
or `set_secret()`; they land in `~/.vigil/secrets.enc`.

Historical `ANTHROPIC_API_KEY` placeholder lines in `.env` are ignored
when the encrypted store has a value.

## `~/.deeptempo/.env` (deprecated)

This was the old default write target of `DotEnvBackend`. It still
*reads* for backward compatibility (position 3 in the backend chain)
but nothing should write here anymore. `scripts/migrate_secrets.py`
moves values from here into `~/.vigil/secrets.enc`; run with `--purge`
to clear it.

## `~/.deeptempo/general_config.json`

Kept for one thing only: the `enable_keyring` flag. You can also set
this via the `ENABLE_KEYRING` env var in `.env`. This file is benign
and will likely be folded into `system_config` in a future cleanup.

## Pointers (DB → secret store)

| Table | Points to |
|---|---|
| `llm_provider_configs.api_key_ref` | a key in the secrets manager (e.g. `llm_provider_anthropic-default_api_key`) |
| `integration_configs` | references secrets by name; values in secrets manager |

Row + value are decoupled on purpose: the DB is safe to back up / copy,
and secrets stay in the encrypted store at rest.

## Bifrost

Bifrost is a sidecar container that fronts all LLM traffic. It has its
own internal state for provider config and cache. Vigil does **not**
rely on Bifrost reading `env.ANTHROPIC_API_KEY` from its docker env
anymore — that was the old flow and caused the "key lost on restart"
problem. Instead:

- On backend startup, `services.bifrost_admin.sync_all_provider_keys()`
  pushes every key in the secrets manager to Bifrost via its admin API
  (`PUT /api/providers/{name}`).
- On provider create/update/delete in the UI, the corresponding endpoint
  in `backend/api/llm_providers.py` pushes the new (or empty) value to
  Bifrost in the same request.

So the flow is: **UI → secrets_manager → bifrost_admin → Bifrost** in
one synchronous chain. No container restart needed to rotate a key.

The seed `docker/bifrost/config.json` still references `env.*` for
first-boot provider/model definitions, but the actual key *values* are
overwritten at runtime.

## Workdirs and logs

- `data/investigations/` — orchestrator working files (investigation
  transcripts, context docs, agent output).
- `data/mitre/`, `data/schemas/` — static reference data.
- `logs/*.log`, `logs/*.pid` — runtime logs and process pids (started
  via `start_web.sh`, `start_daemon.sh`).

## Docker volumes

- `deeptempo-postgres` — Postgres data.
- `deeptempo-redis` — Redis (ARQ queue + rate limiting).
- `deeptempo-bifrost` — Bifrost's internal state (if any persistent).

## Quick reference

| Thing | Where |
|---|---|
| API keys, tokens | `~/.vigil/secrets.enc` |
| Runtime settings (UI-editable) | Postgres `system_config` |
| Bootstrap flags (DB URL, ports, DEV_MODE) | repo `.env` |
| Ephemeral runtime | Redis, Postgres |
| Investigation files | `data/investigations/` |
| Logs, PIDs | `logs/` |
| Legacy secrets (migrate from) | `~/.deeptempo/.env` |
