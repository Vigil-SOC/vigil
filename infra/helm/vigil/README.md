# Vigil SOC Helm chart

Production chart for the Vigil API, daemon, workers, and optional in-cluster
Postgres/Redis.

Full install, values, and troubleshooting:
<https://vigilsoc.org/docs/helm-chart/>

## Postgres passwords

Two passwords, and they must differ.

`secrets.postgresPassword` is the database owner (`postgresql.auth.username`,
`deeptempo` by default). It is stored in its own Secret (`<release>-postgres`)
and mounted only on the in-chart Postgres and the upgrade Jobs. It is not in the
application Secret those pods load with `envFrom`.

`secrets.vigilAppPassword` is the `vigil_app` role. The backend, daemon, workers
and agents log in as that role and receive only this password, as
`POSTGRES_PASSWORD`. The upgrade Jobs set it with `ALTER ROLE vigil_app` on
install and upgrade.

Neither has a default. Set both (`openssl rand -hex 24`). With
`secrets.existingSecret` or `secrets.externalSecret`, that Secret must contain
`POSTGRES_APP_PASSWORD`. Put the owner password in `postgresql.auth.existingSecret`
(or `postgresql.external.existingSecret`) rather than in the application Secret.
If the application Secret also holds `POSTGRES_PASSWORD`, the workload env
replaces that variable with `POSTGRES_APP_PASSWORD` before the process starts.
The chart refuses to render the old published default
`change-me-before-production`, and it refuses to render when the two values
are equal.

A database whose owner password is still that published default must be changed
in Postgres first (`ALTER ROLE` for `postgresql.auth.username`). The chart does
not change the owner password. Set `secrets.postgresPassword` to the new owner
password and `secrets.vigilAppPassword` to a different value; the next upgrade
updates `vigil_app`.

## Schema upgrades

`python -m core.upgrade` from the backend image, logged in as the owner, is the
only thing that writes the schema. App pods only check that the database is at
their image's revision. See [docs/upgrading.md](../../../docs/upgrading.md#helm).

| Value | Default | |
|---|---|---|
| `upgrade.acknowledgeVersion` | `""` | Required on install and every upgrade: this release's version (`appVersion`). A value from an earlier upgrade refuses. |
| `upgrade.acknowledgeReason` | `""` | Required: why there is no Vigil restore point. Helm takes no backup. |
| `upgrade.stopTimeoutSeconds` | `600` | How long the upgrade waits for the release's pods to stop. |
| `upgrade.persistence.*` | 1Gi, cluster default class | `history.jsonl`, mounted on the upgrade Jobs only. Kept after `helm uninstall`. |
| `upgrade.resources` | 100m / 256Mi requests | |
| `upgrade.enabled` | `true` | `false` renders no upgrade Jobs: run the command yourself. |

- **Install.** A post-install Job takes the empty database to head. Do not use
  `--wait`: Helm waits for Ready before running post-install hooks, and the app
  pods are not Ready until the Job has run.
- **Upgrade.** A pre-upgrade Job (weight -10) runs `--check`. When it refuses,
  nothing is stopped and the previous release keeps serving. Then a second Job
  (weight 0) scales every Deployment and StatefulSet of the release except
  Postgres and Redis to 0, pauses KEDA, waits for the pods to go, ends leftover
  `vigil_app` sessions (a non-superuser owner needs `pg_signal_backend` for
  that), and upgrades. Only that Job mounts a token, for a
  namespace Role that can scale and patch ScaledObjects. Nothing is
  cluster-scoped. Give `--timeout` enough time for the whole window.
- **Hook leftovers.** The upgrade ServiceAccount, Role, RoleBinding, ConfigMap,
  NetworkPolicies and PVC are hooks, so `helm uninstall` leaves them. Delete
  them with `kubectl delete sa,role,rolebinding,cm,networkpolicy,pvc -l
  app.kubernetes.io/instance=<release>,app.kubernetes.io/component=upgrade`.
- **Network policies.** With `networkPolicies.enabled`, the upgrade Jobs reach
  the Kubernetes API through `medic.kubeApi.cidrs` and `medic.kubeApi.ports`,
  as Medic does. Set the CIDRs where `lookup` can't run (`helm template`, Argo
  CD, Flux).

## MCP runner

`mcpRunner` (on by default) runs the catalog's MCP servers in their own pod,
each under its own uid, and serves them on port 6991. The backend and daemon
get `MCP_RUNNER_URL` and `MCP_RUNNER_TOKEN`; ingress to the runner is limited
to those two pods when `networkPolicies.enabled` is set.

| Value | Default | |
|---|---|---|
| `mcpRunner.enabled` | `true` | Render the runner Deployment, Service, token Secret and NetworkPolicy. |
| `mcpRunner.image.*` | `<registry>/<namespace>-mcp-runner:<appVersion>` | Built from `infra/docker/Dockerfile.mcp-runner`. |
| `mcpRunner.resources` | 100m / 256Mi requests | |
| `mcpRunner.podSecurityContext`, `mcpRunner.securityContext` | root; `drop: [ALL]`, `add: [SETUID, SETGID, KILL]` | |
| `secrets.mcpRunnerToken` | generated | The runner's bearer, in `<fullname>-mcp-runner`. |
| `detectionRules.volume` | unset | Volume for detection rule repositories: read-write on the backend, read-only on the runner, at `/home/vigil/security-detections`. |

- **Runs as root.** The runner needs root to drop each server to its uid, so the
  pod does not admit in a namespace that enforces the `restricted` Pod Security
  Standard. `baseline` admits it.
- **Token.** Left empty, `secrets.mcpRunnerToken` is generated on install and
  read back on upgrade. `helm template` and GitOps controllers cannot read it
  back and would rotate it on every render: set it explicitly there. With
  `secrets.existingSecret` or `secrets.externalSecret.enabled`, the chart creates
  no runner Secret, and that Secret must carry an `MCP_RUNNER_TOKEN` key.
  Without it the runner pod does not start; the backend and daemon still do.
- **A server on its own runner** is a second runner Deployment plus
  `MCP_RUNNER_URL_<SERVER>` on the backend and daemon. The chart does not
  render that yet.
