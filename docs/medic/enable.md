# Turning Medic on

Medic (the System Watcher) watches this install's own health. It changes nothing in Vigil, and it sends nothing off the machine. It is **off by default** on every install shape.

`VIGIL_MEDIC_ENABLED` is the master switch everywhere (Helm: `medic.enabled`). Medic exits at start when the switch is false. The backend reads the same switch, so the console shows a Medic you turned off as **Off**, not **Down**. Only `true`, `1`, `yes` or `on` turn it on. Anything else counts as off.

With the switch on, one backend process asks Medic for its status once a minute, through the gateway, with the API key, and keeps the answer in Postgres. The console's status is **Running** while Medic answers, and **Down** once it has failed to answer for 5 minutes. A short backend restart keeps it. After 3 minutes or more with nothing polling (the backend down, or Medic switched off), the status starts over: **Unknown** until the next answer, or **Down** about 4 minutes later if Medic still doesn't answer.

On Compose and Helm, Medic reads the backend only through its own read-only gateway. The gateway logs in as a **service account**:

- a Viewer named `medic-` plus 12 random characters (`a-z0-9`), so the name can't be guessed;
- a random 64-character password that only the gateway holds;
- never locked out by failed logins. A lock would let anyone who learns the name blind Medic. Failed logins are still counted and logged.
- The users API won't give it any role except Viewer. No API can make an account a service account.

The account is created by `python -m core.auth.service_account ensure <name>`. It runs in the backend's environment and reads the password from stdin. Running it again with the same password changes nothing. Running it with a new password rotates the password and clears any failed-login count. Any other service account is deactivated, so an old name can't keep working. The command refuses to convert an existing account that belongs to a person.

## Docker Compose

```bash
scripts/medic/enable-compose.sh
```

The script is the whole enable, and it is safe to re-run. It:

1. creates the name, password and Medic API key as `0600` files in `~/.vigil-medic/secrets` (override with `VIGIL_MEDIC_SECRETS_DIR`, an absolute path);
2. writes `compose.env` next to them, holding the switch and this host's settings;
3. builds the Medic images;
4. recreates the backend with the switch on (Vigil's `.env` is passed along, as in the README);
5. creates the service account;
6. starts `medic`, `medic-gateway` and `medic-dockerproxy`.

It never prints a secret. It refuses to run if recreating the backend would leave it without a `JWT_SECRET_KEY`.

After the script, every compose command that should keep Medic needs Medic's settings and overlay. The script prints the exact command:

```bash
docker compose --env-file .env --env-file ~/.vigil-medic/secrets/compose.env \
  -f infra/docker/docker-compose.yml -f infra/docker/medic/docker-compose.medic.yml \
  --profile medic up -d
```

To rotate the password and API key, run `scripts/medic/enable-compose.sh --rotate`. The username stays the same.

To turn Medic off, run `up` without Medic's files. The backend then reads the switch as false and shows **Off**. Medic's volume and data stay.

## Helm (PROVISIONAL)

These steps need the chart's Medic templates (the Medic and gateway Deployments, step S7). Without them, `medic.enabled=true` only sets the backend's switch, and there is no gateway yet to use the account.

1. Before you install or upgrade, create the gateway's password Secret and choose the name. The password is written to a file and never appears on a command line.

   ```bash
   umask 077
   openssl rand -hex 32 > medic-viewer-password
   kubectl -n vigil create secret generic medic-viewer --from-file=password=medic-viewer-password
   rm medic-viewer-password
   NAME="medic-$(head -c 512 /dev/urandom | LC_ALL=C tr -dc a-z0-9 | cut -c1-12)"
   ```

2. Install or upgrade with these values:

   ```
   medic.enabled=true
   medic.gateway.viewer.username=$NAME
   medic.gateway.viewer.passwordSecret.name=medic-viewer
   medic.gateway.viewer.passwordSecret.key=password
   ```

   `medic.enabled` also sets `VIGIL_MEDIC_ENABLED` on the backend.

3. Create the account. The chart's NOTES print this command with your release's names:

   ```bash
   kubectl -n vigil get secret medic-viewer -o jsonpath='{.data.password}' | base64 -d \
     | kubectl -n vigil exec -i deploy/<release>-backend -- python -m core.auth.service_account ensure "$NAME"
   ```

   To rotate, replace the Secret's value, run step 3 again, and restart the gateway Deployment.

## Host-native (`start.sh`)

Host-native Medic is opt-in only. It can reach the backend, Redis and Bifrost on loopback, so it doesn't meet the isolation rule (K1 T-14).

1. Set `VIGIL_MEDIC_ENABLED="true"` in the repo `.env`. The host-run backend reads the switch from there.
2. Run `./start.sh -d`. The first run prints the one-time admin setup block and starts nothing until that setup is done. The block creates the user `vigil-medic`, the directories `/var/lib/vigil-medic` (mode `0700`) and `/opt/vigil-medic`, and one sudoers rule. The installer never creates the user silently.
3. Run `./start.sh -d` again.

Host-native Medic doesn't use the gateway yet, so no service account is needed. If a host-native gateway is added later, it uses the same `ensure` command with Vigil's venv: `venv/bin/python -m core.auth.service_account ensure <name> < password-file`.
