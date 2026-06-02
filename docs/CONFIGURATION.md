# Configuration

## Where do secrets live?

Vigil splits configuration into two stores:

1. **`.env`** — bootstrap-only settings the backend needs before the DB is
   reachable (URLs, ports, dev flags). Nothing sensitive belongs here.
2. **The encrypted secret store** at `~/.vigil/secrets.enc` — every API key,
   token, and password. Written by the web UI (Settings → AI / LLM Providers
   and Settings → Integrations) or by `set_secret()` programmatically. The
   master key sits next to it at `~/.vigil/master.key`.

LLM provider keys (Anthropic, OpenAI, Ollama) are managed entirely through
the UI — they land in the encrypted store and are pushed to Bifrost via its
admin API in the same request. See [STATE.md](STATE.md) for the full secret
inventory.

## `.env`

Copy `env.example` to `.env`:

```bash
cp env.example .env
chmod 600 .env
```

### What goes in `.env`

| Variable | Description |
|----------|-------------|
| `DEV_MODE` | Bypass auth for local development |
| `DATABASE_URL` | PostgreSQL connection (default in docker-compose) |
| `REDIS_URL` | ARQ job queue connection |
| `BIFROST_URL` | LLM gateway address (default `http://bifrost:8080`) |
| `BIND_HOST` / port numbers | Network binding |
| `SECRETS_BACKEND` | `encrypted` (default), `dotenv`, `env`, `keyring` — where new secrets are written |
| `ENABLE_KEYRING` | `false` (default), `true` — include OS keyring in read chain |
| `SENTRY_DSN` | Error reporting endpoint |

### What does NOT go in `.env`

- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / Ollama config — Settings → AI / LLM Providers
- `SPLUNK_PASSWORD`, `CROWDSTRIKE_CLIENT_SECRET`, `VIRUSTOTAL_API_KEY`, etc. — Settings → Integrations
- Any password, token, or private key

Placeholder values for these in `.env` are ignored once the encrypted store
has a value, and a stale placeholder can mask the real key.

## Secrets read priority

When reading, backends are checked in order:
1. Encrypted file (`~/.vigil/secrets.enc`)
2. Process environment variables
3. `.env` file (legacy / interoperability)
4. OS keyring (only if `ENABLE_KEYRING=true`)

## Deployment Examples

### Local Development

```bash
./start.sh
```

### Docker

```bash
docker run --env-file .env vigil
```

### Docker Compose

```yaml
services:
  vigil:
    image: vigil
    ports:
      - "6987:6987"
    env_file:
      - .env
```

### Systemd

```ini
[Service]
EnvironmentFile=/opt/vigil/.env
ExecStart=/opt/vigil/venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 6987
```

### Kubernetes

For server-side deployments, LLM provider keys can be injected as
environment variables at pod start (operator path — distinct from
the local-dev UI path). See [HELM.md](HELM.md) for the recommended
Helm chart values, including `secrets.anthropicApiKey`.

```bash
kubectl create secret generic vigil-secrets \
  --from-literal=DATABASE_URL="postgresql://..."
```

```yaml
envFrom:
  - secretRef:
      name: vigil-secrets
```

## UI Configuration

Most integrations are configured via **Settings > Integrations** in the web UI:

- API endpoints, usernames, non-sensitive config
- Passwords/secrets reference environment variables
- Test connection before saving

## PostgreSQL Setup

Default connection (matches `docker-compose.yml`):

```
postgresql://deeptempo:deeptempo_secure_password_change_me@localhost:5432/deeptempo_soc
```

Start database:

```bash
docker-compose up -d postgres
```

## Security Best Practices

1. Never commit `.env` files to git
2. Use `chmod 600` on secret files
3. Rotate API keys periodically
4. Use separate secrets per environment (dev/staging/prod)
