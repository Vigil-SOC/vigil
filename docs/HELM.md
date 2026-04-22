# Deploying Vigil SOC on Kubernetes

Vigil ships with a Helm chart under [`helm/vigil/`](../helm/vigil/) that
installs the backend, autonomous daemon, LLM worker, Postgres, and Redis as
a single release.

This is the MVP chart — it covers the common single-cluster deployment.
See the "Out of scope" section at the bottom for what it deliberately does
not do yet.

## Prerequisites

- Kubernetes 1.25+
- Helm 3.12+
- A working container registry pull path. Release images are published to
  `ghcr.io/vigil-soc/vigil-backend` and `ghcr.io/vigil-soc/vigil-daemon` by
  the `release.yml` workflow on every `v*.*.*` tag.
- An Anthropic API key

For local testing, [kind](https://kind.sigs.k8s.io/) or
[minikube](https://minikube.sigs.k8s.io/) both work.

## Quick install

```bash
# Production-ish install with in-chart Postgres + Redis
helm install vigil ./helm/vigil \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set secrets.postgresPassword="$(openssl rand -hex 24)" \
  --set secrets.jwtSecretKey="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  --wait --timeout 10m
```

```bash
# Development install — auth bypassed, smaller resource requests
helm install vigil ./helm/vigil \
  -f ./helm/vigil/values-dev.yaml \
  --namespace vigil --create-namespace \
  --set secrets.anthropicApiKey="$ANTHROPIC_API_KEY"
```

## Verifying the install

```bash
kubectl get pods -n vigil
kubectl get jobs -n vigil -l app.kubernetes.io/component=db-init

# End-to-end smoke test
helm test vigil -n vigil

# Port-forward and hit the API
kubectl port-forward -n vigil svc/vigil-backend 6987:6987
curl http://localhost:6987/api/health
```

## Secrets

Two options for secrets:

### 1. Plain values (simplest)

Set `secrets.*` in values.yaml or via `--set`. The chart renders a `Secret`
object named `<release>-secrets` containing the values you pass.

**Do not commit these values.** Use `--set-file`, an external `values.yaml`
that's `.gitignore`d, or (better) option 2 below.

### 2. Pre-created Secret (recommended for prod)

Create the Secret out-of-band (e.g. via
[ExternalSecrets Operator](https://external-secrets.io/), SOPS, or sealed
secrets) and point the chart at it:

```yaml
secrets:
  existingSecret: vigil-prod-secrets
```

The Secret must provide keys matching env var names. At minimum:

- `ANTHROPIC_API_KEY`
- `POSTGRES_PASSWORD`
- `JWT_SECRET_KEY` (when `DEV_MODE=false`)

Plus whichever integrations you use (`SPLUNK_PASSWORD`, `SLACK_BOT_TOKEN`,
`CROWDSTRIKE_CLIENT_ID`, `CROWDSTRIKE_CLIENT_SECRET`, etc.).

## External Postgres / Redis

Disable the in-chart services and point at existing infrastructure:

```yaml
postgresql:
  enabled: false
  external:
    host: db.prod.example.com
    port: 5432
    database: vigil
    username: vigil
    existingSecret: vigil-db-credentials
    existingSecretKey: password
    sslRequired: true

redis:
  enabled: false
  external:
    url: "rediss://:password@redis.prod.example.com:6379/0"
```

## Ingress + TLS

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-body-size: "50m"
  hosts:
    - host: vigil.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: vigil-tls
      hosts:
        - vigil.example.com
```

## How the daemon stays a singleton

The SOC daemon runs as a StatefulSet with `replicas: 1` hardcoded in the
template (not exposed in values.yaml). The orchestrator keeps in-memory
state — work queues, agent tracking — that is not safe to share across
replicas. On rolling updates, the `OrderedReady` + partition strategy ensures
the new pod fully replaces the old one before anything else happens.

If you need horizontal daemon scaling, that's a larger architectural change
and is tracked as a separate follow-up to issue #85.

## How the db-init Job works

On every `helm install` / `helm upgrade`, a Kubernetes Job:

1. Waits for Postgres to become reachable
2. Creates a `_vigil_schema_versions` marker table
3. Applies each SQL file in `.Values.dbInit.sqlFiles` order, skipping ones
   already recorded in the marker table
4. Terminates (TTL = 600s)

The SQL files themselves are copies of `database/init/*.sql`, bundled under
`helm/vigil/files/database-init/` because Helm can only read from inside the
chart directory. The `helm-chart.yml` CI workflow fails the build if these
copies drift from the source.

To add a new init SQL file:

```bash
cp database/init/NEW.sql helm/vigil/files/database-init/
# Then edit helm/vigil/values.yaml and add "NEW.sql" to dbInit.sqlFiles in
# the correct execution order.
```

## Upgrades

```bash
helm upgrade vigil ./helm/vigil \
  -n vigil --reuse-values \
  --set backend.image.tag=v0.2.0 \
  --set daemon.image.tag=v0.2.0 \
  --wait
```

Pod checksums on the ConfigMap + Secret force pod restarts when config
changes, so `helm upgrade --set config.X=Y --reuse-values` will do the right
thing.

## Uninstall

```bash
helm uninstall vigil -n vigil
kubectl delete pvc -n vigil -l app.kubernetes.io/instance=vigil  # optional: drop data
kubectl delete namespace vigil
```

PVCs are **not** auto-deleted with the release — that's a safety measure
against accidental data loss.

## Out of scope for the MVP chart

Tracked as separate follow-ups:

- Bitnami postgresql/redis subcharts (for HA + easier point-in-time restore)
- Native ExternalSecrets Operator templates
- Prometheus ServiceMonitor and pre-canned Grafana dashboards
- NetworkPolicies restricting inter-pod traffic
- HPA based on Redis queue depth (custom metrics adapter)
- Observability stack (OTEL Collector, Jaeger) as optional subcharts
- Splunk / PgAdmin sidecars as profiles

## Troubleshooting

**`db-init` Job fails with "role does not exist"** — the in-chart Postgres
StatefulSet hadn't finished initializing yet. The Job retries (`backoffLimit:
3`); if it still fails, check `kubectl logs -n vigil job/vigil-db-init` and
the Postgres pod logs.

**Daemon pod keeps restarting** — probe the `/health` endpoint directly:
`kubectl exec -n vigil vigil-daemon-0 -- curl http://localhost:9091/health`.
If it returns 200, the probe is misconfigured; if it returns an error or
hangs, the daemon is actually unhealthy (check Anthropic API key, DB
connectivity).

**SPA shows a blank page** — the SPA is bundled into the backend image via
a multi-stage build in `docker/Dockerfile.backend`. If you're using a
custom build of the backend, make sure the multi-stage build step ran and
copied `frontend/build/` into the final image.
