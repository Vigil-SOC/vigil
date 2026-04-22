# Vigil SOC — Helm chart

Production-style Helm chart for the Vigil SOC platform. Ships the backend API
(with bundled SPA), the autonomous SOC daemon, the LLM worker, and
in-cluster Postgres + Redis as a single release.

## TL;DR

```bash
helm install vigil ./helm/vigil \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set secrets.postgresPassword=$(openssl rand -hex 24)
```

For a dev install with auth bypass:

```bash
helm install vigil ./helm/vigil \
  -f ./helm/vigil/values-dev.yaml \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY
```

Then port-forward the backend to try it out:

```bash
kubectl port-forward -n vigil svc/vigil-backend 6987:6987
# open http://localhost:6987
```

## What gets deployed

| Workload | Kind | Replicas | Notes |
|---|---|---|---|
| `vigil-backend` | Deployment | 2 (default) | FastAPI API + bundled SPA on port 6987 |
| `vigil-daemon` | StatefulSet | **1 (singleton)** | Autonomous orchestrator; webhook=8081, metrics=9090, health=9091 |
| `vigil-llm-worker` | Deployment | 2 (default) | ARQ worker for Claude requests off Redis queue |
| `vigil-postgres` | StatefulSet | 1 | Opt-out via `postgresql.enabled=false` |
| `vigil-redis` | StatefulSet | 1 | Opt-out via `redis.enabled=false` |
| `vigil-db-init` | Job (Helm hook) | 1 per install/upgrade | Applies `database/init/*.sql` idempotently |

## Required inputs

At minimum you need:

- `secrets.anthropicApiKey` — Claude API key for AI agents
- `secrets.postgresPassword` — used for the in-chart Postgres; skip if using an external DB with `postgresql.existingSecret`

When `config.DEV_MODE=false` (the default), also set:

- `secrets.jwtSecretKey` — generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`

## External Postgres or Redis

Point the chart at existing infrastructure instead of running in-cluster:

```yaml
postgresql:
  enabled: false
  external:
    host: my-rds.example.com
    port: 5432
    database: vigil
    username: vigil
    existingSecret: my-rds-credentials
    existingSecretKey: password
    sslRequired: true

redis:
  enabled: false
  external:
    url: "rediss://:password@my-elasticache.example.com:6379/0"
```

## Pre-created Secret

If you manage secrets with ExternalSecrets Operator, SOPS, or Sealed Secrets,
create the Secret yourself and point the chart at it:

```yaml
secrets:
  existingSecret: vigil-secrets
```

The Secret must define keys matching env var names:
`ANTHROPIC_API_KEY`, `POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, plus whichever
integration creds you use (`SPLUNK_PASSWORD`, `SLACK_BOT_TOKEN`, …).

## Upgrades

```bash
helm upgrade vigil ./helm/vigil \
  -n vigil --reuse-values \
  --set backend.image.tag=v0.2.0 \
  --set daemon.image.tag=v0.2.0
```

The `db-init` Job re-runs on every upgrade but is idempotent — it tracks
applied files in a `_vigil_schema_versions` table.

## Values reference

See `values.yaml` for the full schema. Non-obvious choices:

- **Daemon singleton**: `replicas: 1` is hardcoded in `daemon-statefulset.yaml`
  because the orchestrator holds in-memory state. Do not template this.
- **LLM worker image**: inherits from `backend.image` unless
  `llmWorker.image.repository` is set. The only difference at runtime is the
  entrypoint (`services.run_llm_worker`).
- **Daemon probes**: target port `9091` (`/health`), not `9090`. Port `9090`
  is the Prometheus `/metrics` port, which is only served when
  `config.VIGIL_OTEL_ENABLED=true`.

## Out of scope (today)

- Bitnami postgresql/redis subcharts
- ExternalSecrets Operator native support (use `secrets.existingSecret` instead)
- Prometheus ServiceMonitor
- NetworkPolicies
- Queue-depth HPA (CPU-based only)
- Observability (OTEL Collector, Jaeger, Grafana)
- Splunk / PgAdmin sidecar profiles

These are tracked as follow-ups to issue #85.

## Development

```bash
# Lint
helm lint helm/vigil
helm lint helm/vigil -f helm/vigil/values-dev.yaml

# Render without applying
helm template vigil helm/vigil \
  --set secrets.anthropicApiKey=test \
  --set secrets.postgresPassword=test

# Dry-run install
helm install --dry-run --debug vigil helm/vigil \
  --set secrets.anthropicApiKey=test \
  --set secrets.postgresPassword=test
```

### Keeping SQL files in sync

The chart bundles copies of `database/init/*.sql` under
`helm/vigil/files/database-init/`. CI (`.github/workflows/helm-chart.yml`) will
fail on drift. To sync after adding new init SQL:

```bash
cp database/init/*.sql helm/vigil/files/database-init/
# then add the new filename to values.yaml -> dbInit.sqlFiles in order
```
