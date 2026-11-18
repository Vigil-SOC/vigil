# AWS credentials for the Amazon Security Lake server

The Security Lake MCP server signs in with the default AWS chain of the process
it runs in. Under the MCP runner, that process gets only credential *pointers*
from the runner's environment: `AWS_PROFILE`, `AWS_CONFIG_FILE`,
`AWS_SHARED_CREDENTIALS_FILE`, `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE`,
the `AWS_CONTAINER_*` variables, `AWS_REGION` and `AWS_DEFAULT_REGION`
(`AWS_POINTER_VARS` in `core/integrations/mcp/child_env.py`). It never gets
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, and no
other server gets any of them.

The runner gives each server its own uid, with no supplementary groups. A file
that a pointer names (a token, a config, a credentials file) is readable by the
runner's root process but usually not by that uid. So the runner reads the file
as root and writes a private copy, as the server's uid and mode 0600, into the
server's own 0700 HOME (`/var/lib/vigil/mcp/amazon-security-lake/.aws-vigil/`).
It then points the variable at the copy, and re-reads the source every 60 s, so
a rotated token or an edited config carries through. Only paths set in the
runner's own environment are copied, and only regular files up to 1 MiB.
Paths written *inside* a config file (`web_identity_token_file`,
`credential_process`) are not copied. Set `AWS_WEB_IDENTITY_TOKEN_FILE` for
those instead.

The Settings test runs on the backend's chain, not the runner's. Give both the
same source. If the backend resolves static keys from its environment, the test
says the server will not receive them and runs no discovery.

## Compose

On an EC2 host with an instance role, nothing needs configuring.

For a shared config, mount it into `mcp-runner` read-only and name it in `.env`.
The runner has no `DAC_OVERRIDE`, so root in the container can read the file only
if root owns it. Uids for the servers come from their sorted position in the
catalog (`services/mcp_runner/catalog.py` `assign_uids`) and can change on
upgrade, so don't chown the file to one of them.

```sh
sudo install -d -m 0755 /etc/vigil/aws
sudo install -m 0640 -o root -g 1000 config /etc/vigil/aws/config   # 0600 root:root if only the runner reads it
```

```yaml
# docker-compose.override.yml
services:
  mcp-runner:
    volumes:
      - /etc/vigil/aws:/etc/vigil/aws:ro
  backend:            # so the Settings test reads the same source (uid 1000)
    volumes:
      - /etc/vigil/aws:/etc/vigil/aws:ro
    environment:
      AWS_CONFIG_FILE: /etc/vigil/aws/config
      AWS_PROFILE: ${AWS_PROFILE:-}
```

```sh
# .env
AWS_CONFIG_FILE=/etc/vigil/aws/config
AWS_PROFILE=security-lake
```

Group 1000 is the backend's user. Every server runs with its own uid as its only
group, so `0640 root:1000` gives no server access to the source file.

## Helm

- **EKS IRSA or Pod Identity.** Annotate `serviceAccount` with the role, or add
  a Pod Identity association for it. The backend uses the same service account.
  The EKS webhook injects its projected token volume and `AWS_*` env into the
  runner even with `automountServiceAccountToken: false`. The token is projected
  root-owned 0600 because the runner pod has no `fsGroup`, and the runner copies
  it as described above.
- **No webhook.** Project the token yourself with `mcpRunner.extraVolumes` /
  `extraVolumeMounts` and set `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE` in
  `mcpRunner.extraEnv`. `values.yaml` has the example.
- **Shared config.** Mount a Secret through `mcpRunner.extraVolumes` with
  `defaultMode: 0400` and set `AWS_CONFIG_FILE` in `mcpRunner.extraEnv`. The
  backend chart has no matching volume hook, so the Settings test does not see
  that config. Prefer IRSA or Pod Identity on Helm.
