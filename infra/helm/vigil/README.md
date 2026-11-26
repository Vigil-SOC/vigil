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

`secrets.vigilAppPassword` is the `vigil_app` role. The backend, daemon and
workers log in as that role and receive only this password, as
`POSTGRES_PASSWORD`. The upgrade Jobs set it with `ALTER ROLE vigil_app` on
install and upgrade.

`secrets.vigilAgentPassword` is the `vigil_agent` role. agent-serve and
agent-worker log in as that role, which holds only the agent ledger, run leases
and directives, and receive this password by name. They do not load the
application Secret. The upgrade Jobs set it beside `vigil_app`'s.

None has a default, except that an unset `secrets.vigilAgentPassword` is
generated once and kept across upgrades with `lookup` (set it under
`helm template` or a GitOps controller, where `lookup` sees nothing). Set all
three (`openssl rand -hex 24`). With
`secrets.existingSecret` or `secrets.externalSecret`, that Secret must contain
`POSTGRES_APP_PASSWORD` and `POSTGRES_AGENT_PASSWORD`. Put the owner password in `postgresql.auth.existingSecret`
(or `postgresql.external.existingSecret`) rather than in the application Secret.
If the application Secret also holds `POSTGRES_PASSWORD`, the workload env
replaces that variable with `POSTGRES_APP_PASSWORD` before the process starts.
The chart refuses to render the old published default
`change-me-before-production`, and it refuses to render when any two of the
three values are equal.

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
- **Hook leftovers.** The upgrade Jobs, ServiceAccount, Role, RoleBinding,
  ConfigMap, NetworkPolicies and PVC are hooks, so `helm uninstall` leaves
  them. Delete them with `kubectl delete job,sa,role,rolebinding,cm,networkpolicy,pvc
  -l app.kubernetes.io/instance=<release>,app.kubernetes.io/component=upgrade`.
  The Jobs go first or the PVC stays `Terminating`: a finished Job's pods still
  hold it until they are deleted, which the Jobs do themselves 15 minutes after
  they finish. Deleting the PVC deletes `history.jsonl`, the upgrade record.
- **Network policies.** With `networkPolicies.enabled`, the upgrade Jobs reach
  the Kubernetes API through `medic.kubeApi.cidrs` and `medic.kubeApi.ports`,
  as Medic does. Set the CIDRs where `lookup` can't run (`helm template`, Argo
  CD, Flux).

## Agent shell

`agentShell` (on by default) runs the read-only case shell behind the backend's
`read_case_shell` tool: the agent image's `node dist/shell.js`, on port 6992. The
backend gets `SHELL_URL` and `SHELL_TOKEN`; with the shell off the tool leaves
the catalogue.

| Value | Default | |
|---|---|---|
| `agentShell.enabled` | `true` | Render the shell Deployment, Service and NetworkPolicy. |
| `agentShell.replicas` | `1` | Each replica runs up to 8 calls at once. |
| `agentShell.resources` | 50m / 128Mi requests, 1Gi limit | The limit also bounds `/work`, an in-memory emptyDir. |
| `secrets.agentShellToken` | generated | The bearer the backend presents to the shell, in `<fullname>-agent-shell`. |

- **No network but the backend's calls.** Its NetworkPolicy admits backend pods
  on 6992 and denies all egress, and renders whatever `networkPolicies.enabled`
  says; the chart's `-deny-all` policy leaves the shell out, as its open egress
  would undo that. Only a network plugin that enforces NetworkPolicy makes this
  real.
- **Token.** `POST /run` requires `Authorization: Bearer $SHELL_TOKEN`, so a pod
  the policy fails to keep out still cannot run commands. Generated and kept as
  `secrets.mcpRunnerToken` is (set it under `helm template` or GitOps). With
  `secrets.existingSecret` or `secrets.externalSecret.enabled` that Secret must
  carry a `SHELL_TOKEN` key; without it the shell pod does not start and
  `read_case_shell` reports unavailable. The token opens nothing but the shell.
- **Runs as root** with only SETUID, SETGID and KILL, to drop each call to its
  own uid: `baseline` Pod Security admits it, `restricted` does not. No
  credentials or service account token are mounted; `SHELL_TOKEN` is its only
  secret, and no command it runs sees it.

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

## Declared intent directory

Set `VIGIL_INTENT_DIR` on the daemon to a directory holding `INTENT.md` and
`REVISION`, and the daemon applies each new revision without a restart. Unset,
`INTENT.md` stays observe-only. The daemon is the one applier, and the backend
reads what it applied from the database; mount the directory elsewhere only
for skills (below).

| Value | Default | |
|---|---|---|
| `daemon.extraVolumes` | `[]` | The volume holding the directory. |
| `daemon.extraVolumeMounts` | `[]` | Where the daemon sees it; read-only is enough. |
| `daemon.extraEnv` | `[]` | `VIGIL_INTENT_DIR`, the mount path. |

```yaml
daemon:
  extraVolumes:
    - name: intent
      configMap:
        name: vigil-intent   # INTENT.md and REVISION, rendered by Flux or Argo CD
  extraVolumeMounts:
    - name: intent
      mountPath: /etc/vigil/intent
      readOnly: true
  extraEnv:
    - {name: VIGIL_INTENT_DIR, value: /etc/vigil/intent}
```

- **Revision.** The first line of `REVISION` names it, and the change reason of
  each `config.change` entry the apply writes says it. Vigil does not read
  `.git` and does not verify it. A GitOps controller writes the commit into the
  ConfigMap; a plain checkout writes it in a post-merge hook
  (`git rev-parse HEAD > REVISION`). Without the file the revision is `unknown`.
- **Polling.** The daemon re-reads both files every 30 seconds and acts only
  when their content changes, so a ConfigMap's symlink swap is picked up.
- **What a revision does.** It is rejected whole, naming the file and line, if
  the frontmatter does not parse, names an unknown key or has a value of the
  wrong type. Otherwise only the keys whose declared value changed are written;
  a tightening applies at once, a loosening is recorded as pending and waits
  for a person. A missing directory keeps the last revision, state `degraded`.
  `GET /api/config/intent` shows the revision, its state and the pending keys.

- **Intent without `INTENT.md`.** A directory with no `INTENT.md` reads intent
  as `degraded`; an `INTENT.md` with empty front matter declares nothing.

### Site context in the declared directory

Files under `site/*.md` in the same directory declare zones, crown jewels,
owners and blind spots by selector (format and rules: the "Enforced: a
declared directory" section of the repo's `INTENT.md`). The daemon applies
them in the same poll, on their own verdict, to the `site.applied` row, with
or without `INTENT.md`; a refused site revision names the file and line and
keeps the last good one, and never holds up an `INTENT.md` change. Unsetting
`VIGIL_INTENT_DIR` clears the site at the daemon's next start.
`GET /api/config/site` and the /mcp tool `get_site_context` (`platform.read`)
show what runs. A ConfigMap is flat, so give the files keys such as
`site-zones.md` and mount them with `items`:

```yaml
daemon:
  extraVolumes:
    - name: intent
      configMap:
        name: vigil-intent
        items:
          - {key: INTENT.md, path: INTENT.md}
          - {key: REVISION, path: REVISION}
          - {key: site-zones.md, path: site/zones.md}
```

### Skills in the declared directory

Keep skills in the same checkout (`skills/<name>/SKILL.md`) and point
`VIGIL_SKILLS_PATH` under the mount, and they are managed by it: the Skills
screen and `GET /api/skills` show the directory and its revision, and Vigil
refuses every save, upload, delete, repo install and library update there.
They change by commit; a changed skill is held until its scan clears it, as
any other. Every process that loads skills needs the mount and both settings:

```yaml
config:
  VIGIL_SKILLS_PATH: /etc/vigil/intent/skills
backend:
  extraVolumes: [{name: intent, configMap: {name: vigil-intent}}]
  extraVolumeMounts: [{name: intent, mountPath: /etc/vigil/intent, readOnly: true}]
  extraEnv: [{name: VIGIL_INTENT_DIR, value: /etc/vigil/intent}]
llmWorker:
  extraVolumes: [{name: intent, configMap: {name: vigil-intent}}]
  extraVolumeMounts: [{name: intent, mountPath: /etc/vigil/intent, readOnly: true}]
# daemon: as above.
```

A ConfigMap is flat, so a skill folder needs a volume that keeps directories
(a git-sync sidecar's emptyDir, or a ReadOnlyMany claim) rather than one
ConfigMap.
