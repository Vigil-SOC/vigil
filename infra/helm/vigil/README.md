# Vigil SOC Helm chart

Production chart for the Vigil API, daemon, workers, and optional in-cluster
Postgres/Redis.

Full install, values, and troubleshooting:
<https://vigilsoc.org/docs/helm-chart/>

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
