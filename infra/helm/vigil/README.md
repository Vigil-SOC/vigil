# Vigil SOC Helm chart

Production chart for the Vigil API, daemon, workers, and optional in-cluster
Postgres/Redis.

Full install, values, and troubleshooting:
<https://vigilsoc.org/docs/helm-chart/>

## Postgres passwords

Two passwords, and they must differ.

`secrets.postgresPassword` is the database owner (`postgresql.auth.username`,
`deeptempo` by default). It is stored in its own Secret (`<release>-postgres`)
and mounted only on the in-chart Postgres and the db-init Job. It is not in the
application Secret those pods load with `envFrom`.

`secrets.vigilAppPassword` is the `vigil_app` role. The backend, daemon, workers
and agents log in as that role and receive only this password, as
`POSTGRES_PASSWORD`. The db-init Job sets it with `ALTER ROLE vigil_app` on
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
password and `secrets.vigilAppPassword` to a different value; db-init updates
`vigil_app`.

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
