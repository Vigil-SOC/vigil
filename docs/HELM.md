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
helm upgrade vigil ./helm/vigil -n vigil --reuse-values --wait
```

The chart's default image tag resolves to `Chart.AppVersion`, which
release-please bumps in lockstep with the chart `version` on every
release — so a `helm upgrade` after pulling the new chart picks up the
matching images automatically. Override only if you need to pin to a
different tag than the chart's `appVersion` (for example, to deploy a
`:latest` build for testing):

```bash
helm upgrade vigil ./helm/vigil -n vigil --reuse-values --wait \
  --set backend.image.tag=latest \
  --set daemon.image.tag=latest
```

Pod checksums on the ConfigMap + Secret force pod restarts when config
changes, so `helm upgrade --set config.X=Y --reuse-values` will do the right
thing.

> ⚠️ **First upgrade when `dbInit.sqlFiles` has changed** — `helm
> upgrade --reuse-values` reuses the *previous release's* coalesced
> values, which means a longer `dbInit.sqlFiles` list in the new chart
> is silently overwritten by the previous (shorter) one. Any new SQL
> files in the bump won't run, and code that touches their tables
> crashes at runtime. On the first upgrade after a chart bump that
> added init SQL, use one of:
>
> ```bash
> # Helm 3.14+ — reset to new defaults, then layer user overrides on top
> helm upgrade vigil ./helm/vigil -n vigil --reset-then-reuse-values --wait
>
> # Or pass an explicit values file so the new defaults aren't lost
> helm upgrade vigil ./helm/vigil -n vigil -f my-values.yaml --wait
> ```
>
> Subsequent upgrades that don't touch `dbInit.sqlFiles` can go back to
> plain `--reuse-values`.

## Uninstall

```bash
helm uninstall vigil -n vigil
kubectl delete pvc -n vigil -l app.kubernetes.io/instance=vigil  # optional: drop data
kubectl delete namespace vigil
```

PVCs are **not** auto-deleted with the release — that's a safety measure
against accidental data loss.

## NetworkPolicies

Flip `networkPolicies.enabled=true` to lock down inter-pod traffic. The chart
emits a default-deny policy plus per-component allow rules:

- Postgres / Redis: accept only from backend, daemon, llm-worker, and the
  db-init Job
- Daemon webhook (port 8081): restricted to CIDRs listed in
  `networkPolicies.daemon.webhookAllowFrom` — empty list means
  cluster-internal only
- Backend: accepts traffic from the ingress controller's namespace (by
  label selector) + cluster-internal

```yaml
networkPolicies:
  enabled: true
  ingressControllerNamespaceSelector:
    kubernetes.io/metadata.name: ingress-nginx
  daemon:
    webhookAllowFrom:
      - 203.0.113.0/24   # your SIEM's outbound CIDR
```

When Bitnami postgresql / redis subcharts are active, their own
`networkPolicy.*` settings take over; the chart's NetworkPolicy for the
corresponding data service suppresses itself.

## Observability

### ServiceMonitor (Prometheus Operator)

```yaml
observability:
  serviceMonitor:
    enabled: true
    labels:
      release: kube-prometheus-stack  # match your Prometheus instance's selector
```

The daemon's `/metrics` endpoint only serves traffic when
`config.VIGIL_OTEL_ENABLED=true` — flip both together.

### In-chart OTEL Collector

Ships the upstream `open-telemetry/opentelemetry-collector` chart as an
optional subchart. Requires `helm dependency update` once:

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm dependency update helm/vigil
```

Then enable:

```yaml
otelCollector:
  enabled: true
  # Replace the default debug exporter with whatever your stack consumes
  config:
    exporters:
      otlphttp/jaeger:
        endpoint: http://jaeger-collector:4318
    service:
      pipelines:
        traces:
          exporters: [otlphttp/jaeger]

config:
  VIGIL_OTEL_ENABLED: "true"
  # Auto-rewritten to http://<release>-opentelemetry-collector:4317 when
  # otelCollector.enabled=true, but you can override if needed.
```

## Autoscaling the LLM worker

Two modes — pick one.

### CPU-based (no extra dependencies)

```yaml
llmWorker:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 20
    targetCPUUtilizationPercentage: 70
```

### KEDA-based (queue-depth driven)

Recommended for Vigil workloads: LLM calls are I/O-bound, so CPU is a poor
proxy for load. KEDA watches the `arq:llm` Redis list and scales when the
backlog grows.

Prerequisite: KEDA installed on the cluster (https://keda.sh/docs/latest/deploy/).

```yaml
llmWorker:
  autoscaling:
    enabled: false   # disable the CPU HPA
    keda:
      enabled: true
      minReplicas: 2
      maxReplicas: 20
      listLength: "5"   # 1 replica per 5 queued items
```

Works with all three Redis backends (in-chart, Bitnami subchart, external).
When the Bitnami subchart is active, the chart also emits a KEDA
`TriggerAuthentication` referencing the Bitnami-generated Secret.

## Choosing a Postgres/Redis backend

Three modes, mutually exclusive:

| Mode | When | How |
|---|---|---|
| MVP in-chart StatefulSet | default, dev, small deployments | default values |
| Bitnami subchart | production, want HA / metrics exporters / replicas | `postgresql.bitnami.enabled=true` + `redis.bitnami.enabled=true` |
| External | managed DB (RDS, Aurora, ElastiCache, etc.) | `postgresql.enabled=false` + `postgresql.external.*` |

Bitnami is opt-in because it adds ~2MB of subchart assets and requires
`helm dependency update`. Run once after cloning:

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency update helm/vigil
```

Then enable in values:

```yaml
postgresql:
  enabled: false         # disable MVP StatefulSet
  bitnami:
    enabled: true
    auth:
      database: deeptempo_soc
      username: deeptempo
    primary:
      persistence:
        size: 100Gi
      resources: { requests: { cpu: "1", memory: "2Gi" } }

redis:
  enabled: false
  bitnami:
    enabled: true
    architecture: replication
    auth:
      enabled: true
```

## Development utilities (Splunk, pgAdmin)

Off by default; flip on for demo clusters or local testing only:

```yaml
splunk:
  enabled: true    # ClusterIP Service on 8000 (web), 8088 (HEC), 8089 (mgmt)
  persistence:
    size: 50Gi

pgadmin:
  enabled: true    # ClusterIP Service on port 80
```

Neither is exposed via Ingress — use `kubectl port-forward` to reach them.
NOTES.txt warns if either is enabled alongside `config.DEV_MODE=false`.

## Secrets — advanced patterns

See [HELM-SECRETS.md](./HELM-SECRETS.md) for end-to-end workflows covering
[Bitnami SealedSecrets](https://github.com/bitnami-labs/sealed-secrets) and
[Mozilla SOPS](https://github.com/getsops/sops), plus a comparison of all
four supported secret-management patterns.

## Out of scope (today)

Not yet implemented; contributions welcome:

- Pre-canned Grafana dashboards
- Vertical Pod Autoscaler (VPA) support
- Multi-region deployment patterns

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
