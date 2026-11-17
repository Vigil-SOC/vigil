# Turning Medic on

Medic (the System Watcher) watches this install's own health. It changes nothing in Vigil, and it sends nothing off the machine. It is **off by default** on every install shape.

`VIGIL_MEDIC_ENABLED` is the master switch everywhere (Helm: `medic.enabled`). Medic exits at start when the switch is false. The backend reads the same switch, so the console shows a Medic you turned off as **Off**, not **Down**. Only `true`, `1`, `yes` or `on` turn it on. Anything else counts as off.

With the switch on, one backend process asks Medic for its status once a minute, through the gateway, with the API key, and keeps the answer in Postgres. The console's status is **Running** while Medic answers, and **Down** once it has failed to answer for 270 seconds (4½ minutes), so a stopped Medic shows Down inside 5 minutes. A short backend restart keeps it. After 3 minutes or more with nothing polling (the backend down, or Medic switched off), the status starts over: **Unknown** until the next answer, or **Down** about 3½ minutes later if Medic still doesn't answer.

On Compose and Helm, Medic reads the backend only through its own read-only gateway. The gateway reads as a **service account**:

- a Viewer named `medic-` plus 12 random characters (`a-z0-9`), so the name can't be guessed;
- an API token that only the gateway holds, minted from the host by `python -m core.auth.service_account token <name>`, with no password at all; a site enabled before tokens that hasn't moved yet (below) still reads on a random 64-character password;
- never locked out by failed logins. A lock would let anyone who learns the name blind Medic. Failed logins are still counted and logged.
- The users API won't give it any role except Viewer, and no one can mint it a token through the console or the API: only the host command below can. Other service accounts are made on Settings › System › Users, are token-only and have an accountable person; none of them is Medic's.

On Compose and Helm the account is created by `token` (below). The older password form, `python -m core.auth.service_account ensure <name>`, refuses (exit 77) once the account has a token. It runs in the backend's environment and reads the password from stdin. Running it again with the same password changes nothing. Running it with a new password rotates the password and clears any failed-login count. Any other active Medic service account is deleted (its principal is marked deleted and the audit trail records each one), so an old name can't keep working. The command never reactivates an account: if Medic's account was deactivated, say during an incident, it refuses with exit code 77 (`<name> is deactivated; reactivate it on Team & access`) and changes nothing, and it never acts on a deleted one. A different name is refused the same way while the newest Medic account is deactivated and not deleted: exit 77, nothing created or retired, `Medic's account <name> was deactivated on Team & access; reactivate it there to turn Medic back on`. An older inactive Medic account never blocks a new name, and a current account that was deleted does not either. It refuses to convert an existing account that belongs to a person.

`python -m core.auth.service_account token <name>` is the token-only form. It creates the account with no password if it is missing, refuses on the same grounds as `ensure`, rotates the account's token (the old one keeps working for 24 hours) or mints one, and prints only the token on stdout. A password the account still has stops working the first time the backend accepts the token, and `ensure` refuses an account that has ever had a token.

## Docker Compose

Medic's overlay needs **Docker Engine 28.0 or later** (tested on 28.0.4 and 29.9.3) with Compose v2. Its two networks are internal bridges that the host takes no address on (`com.docker.network.bridge.inhibit_ipv4`), so nothing on the host listening on every interface, such as sshd or a published port, can be reached from Medic or the agents. A network created by an older overlay gets the option when Compose recreates it on the next `up`.

```bash
scripts/medic/enable-compose.sh
```

The script is the whole enable, and it is safe to re-run. It:

1. creates the name and Medic API key as `0600` files in `~/.vigil-medic/secrets` (override with `VIGIL_MEDIC_SECRETS_DIR`, an absolute path);
2. writes `compose.env` next to them, holding the switch and this host's settings (unquoted `KEY=value` lines; from here on `start.sh` and the console add Medic's overlay, even after `--secrets-only`);
3. builds the Medic images;
4. recreates the backend with the switch on (Vigil's `.env` is passed along, as in the README);
5. mints the gateway's token into `medic_gateway_token` (`0600`), creating the service account if it is missing. The command's output goes straight into the file, never to the terminal or a command line, and a failed mint leaves the old file as it was. A file that already holds a token is kept: a plain re-run mints nothing;
6. starts `medic`, `medic-gateway` and `medic-dockerproxy`, recreating the gateway when its token is new.

It never prints a secret; its summary says whether the token was minted, rotated or kept, or is still to do (`--secrets-only` mints nothing). It refuses to run if recreating the backend would leave it without a `JWT_SECRET_KEY`.

On Linux the script also hands each file to whoever reads it, since Compose bind-mounts secret files as they are: the token (and an upgraded site's password) to the gateway (uid 10002), and the API key to `root:10010`, mode `0640`. Group 10010 belongs to Medic and the backend only (`group_add`), so the key isn't readable through gid 1000, which on Linux is often a person's own group. The gateway passes the key through without reading the file.

After the script, `./start.sh` (through `scripts/lib.sh`'s `dc`) and the console's service controls add Medic's overlay and settings themselves whenever `compose.env` exists, so a restart through them keeps Medic on its network. A `docker compose` command you type yourself needs them too. The script prints the exact command:

```bash
docker compose --env-file .env --env-file ~/.vigil-medic/secrets/compose.env \
  -f infra/docker/docker-compose.yml -f infra/docker/medic/docker-compose.medic.yml \
  --profile medic up -d
```

Without the overlay, the next `up` of the daemon or an agent takes it off Medic's network, and Medic reads DNS errors from then on: it goes blind, which it reports as "can't see", not as an incident.

To rotate the token and API key, run `scripts/medic/enable-compose.sh --rotate`. The username stays the same. The old token keeps working for 24 hours, so Medic keeps reading while the gateway is recreated. `--rotate --secrets-only` writes the new API key now and leaves the token to the next full run.

**Sites enabled before tokens.** Such a site has a `viewer_password` file and no token. It keeps reading on the password with no step on upgrade day: the gateway's token file and password file are each optional, and `./start.sh` and the console's service controls (through `scripts/lib.sh`'s `dc`) create an empty placeholder for a missing one, which the gateway treats as absent. A `docker compose` command you type yourself doesn't: before the first one after upgrading, run `./start.sh` or the enable script once, or create the empty `0600` file `medic_gateway_token` in the secrets directory yourself. Until it moves, holders of `medic.admin` see a bell line saying Medic still reads with a password, with the command that moves it. To move the site, re-run `scripts/medic/enable-compose.sh`. It mints the token and recreates the gateway; the gateway reads with the token from then on, and the backend ends the account's password the first time it accepts the token. The script never makes or rotates `viewer_password` any more, and leaves the file in place: the gateway doesn't read it while a token is readable, and a later release deletes it. The password path is a fallback for an announced window, and is removed no earlier than the release that ends that window.

**After a restore.** A database restored from before the token was minted doesn't know it, and a restore from before the move brings the password back. Mint again: delete `medic_gateway_token` from the secrets directory and re-run `scripts/medic/enable-compose.sh` (or run it with `--rotate`, which also rotates the API key).

To turn Medic off, delete `compose.env` (that file is what makes `start.sh` and the console add Medic's overlay), then run `up` without Medic's files. The backend then reads the switch as false and shows **Off**. Medic's volume and data stay.

## Helm (PROVISIONAL)

The chart needs a cluster whose network plugin **enforces NetworkPolicy** (Calico, Cilium, kind's default kindnet, or a managed cluster with enforcement on). Medic checks at start and refuses to run (exit 3) where nothing enforces it, e.g. flannel. Plugins set to reject denied traffic rather than drop it are fine.

The chart mints Medic's API key at install into the Secret `<release>-medic-api-key` and keeps it across upgrades; Medic and the backend mount it as a file. The backend polls `http://<release>-medic-gateway-in:8470` once a minute and shows **Down** after 270 seconds without an answer. Where Helm can't read the cluster when it renders (`helm template`, Argo CD, Flux), create the Secret yourself (key `api_key`, 32 random bytes base64url) and set `medic.apiKey.existingSecret`; otherwise every render mints a new key. For the same reason `helm diff` and a client-side `helm upgrade --dry-run` always show the key as changed; a real `helm upgrade` keeps it.

The gateway reads its API token from the Secret `<release>-medic-gateway-token` (key `token`; set `medic.gateway.tokenSecret.name` and `.key` to use another). The chart mounts it as a file but never creates, fills or overwrites it, so `helm upgrade` keeps what you put there. Until the Secret exists the gateway runs and reports `no_credential` on its `/_gw/status`.

1. Install or upgrade with these values. No Secret needs to exist first.

   ```
   medic.enabled=true
   medic.gateway.viewer.username=medic-<12 of a-z0-9>
   ```

   Choose the name once and keep it, for example `NAME="medic-$(head -c 512 /dev/urandom | LC_ALL=C tr -dc a-z0-9 | cut -c1-12)"`. `medic.enabled` also sets `VIGIL_MEDIC_ENABLED` on the backend.

2. Once the backend is up, mint the token and store it. The chart's NOTES print this command with your release's names:

   ```bash
   kubectl -n vigil exec deploy/<release>-backend -- python -m core.auth.service_account token "$NAME" \
     | kubectl -n vigil create secret generic <release>-medic-gateway-token --from-file=token=/dev/stdin --dry-run=client -o yaml \
     | kubectl -n vigil apply -f -
   ```

   The command creates the account if it is missing. The token travels on a pipe, never on a command line. The kubelet refreshes the mounted file within about a minute, and the gateway picks it up on its next request with no restart; its `/_gw/status` then shows `credential: token`.

   To renew the token (the console warns before it expires), run the same command again. The old token keeps working for 24 hours, so the gateway never goes dark while the file catches up.

**GitOps and external secret stores.** Argo CD, Flux, External Secrets or Sealed Secrets can fill the Secret their own way: run `python -m core.auth.service_account token <name>` in the backend once, put its output in your store, and have it sync to the Secret above (or name yours with `medic.gateway.tokenSecret.name`). Don't make the chart's release own it.

**A site set up before tokens.** An earlier chart required `medic.gateway.viewer.passwordSecret` and a password set with `ensure`. Upgrade with the same values: the password Secret is still mounted (now optional), and the gateway keeps reading on the password while no token is there. Then run the command in step 2. The gateway moves to the token on its next request, and the account's password stops working once the backend first accepts the token. Until then, holders of `medic.admin` see a bell line saying Medic still reads with a password. You can then drop `medic.gateway.viewer.passwordSecret` from your values and delete that Secret.

## Host-native (`start.sh`)

Host-native Medic is opt-in only. It can reach the backend, Redis and Bifrost on loopback, so it doesn't meet the isolation rule (K1 T-14). `./start.sh -d` prints that warning every time Medic is on.

**Before you start, check the host:**

- **Not under DEV_MODE.** With `DEV_MODE` on, every backend request is an admin with no login, so `./start.sh -d` refuses to start Medic (K1 T-37) and says so. Vigil itself still starts.
- **A supported OS.** Medic builds its own venv from `services/medic/uv.lock`. Its regex library (`google-re2`) ships prebuilt wheels only for **glibc 2.28 or later** (RHEL 8, Debian 10, Ubuntu 20.04 and later; the locked wheels are tagged `manylinux_2_27` x86_64 and `manylinux_2_26` aarch64) and **macOS 13 or later**, Python 3.12. There are no musl (Alpine) wheels. Elsewhere `uv` falls back to building from source, which needs a C++ compiler and the RE2/Abseil headers, and Medic usually doesn't start ("building its venv … failed").
- **`./start.sh -d`, not foreground.** Only daemon mode runs Medic's restart loop.

**Steps:**

1. Set `VIGIL_MEDIC_ENABLED="true"` in the repo `.env`. The host-run backend reads the switch from there.
2. Run `./start.sh -d`. With Medic on and its setup missing, it prints a one-time admin setup block, starts Vigil, and leaves Medic off. The installer and `start.sh` **never** create the OS user, its directories or the sudo rule: an admin runs the block, as printed. On Linux it looks like this (`<you>` is the user that runs Vigil):

   ```bash
   id vigil-medic >/dev/null 2>&1 || sudo useradd --system --user-group \
       --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin vigil-medic
   sudo install -d -o vigil-medic -g vigil-medic -m 0700 /var/lib/vigil-medic
   sudo install -d -o <you> -m 0755 /opt/vigil-medic
   echo '<you> ALL=(vigil-medic) NOPASSWD: /opt/vigil-medic/bin/medic-loop' | sudo tee /etc/sudoers.d/vigil-medic >/dev/null
   sudo chmod 0440 /etc/sudoers.d/vigil-medic && sudo visudo -cf /etc/sudoers.d/vigil-medic
   ```

   On macOS the user is created with `dscl` instead (a hidden user and group with a free id below 500, shell `/usr/bin/false`, home `/var/empty`), and the data directory is `/Library/Application Support/vigil-medic`. Use the block `start.sh` prints: it picks the free id on your machine, and it skips the user lines if `vigil-medic` already exists, so an existing user is never re-numbered.

   The sudo rule lets `<you>` run exactly one program as `vigil-medic`: Medic's restart loop. It also means that anyone who controls `<you>` can act as `vigil-medic` (disclosed in K4).
3. Run `./start.sh -d` again. Before starting Medic it checks, as `vigil-medic`, that Medic can't read Vigil's secrets: `~/.vigil/master.key`, `secrets.enc`, `jwt_secret` and the repo `.env`. If it can read any of them, Medic isn't started and the exact fix is printed, for example:

   ```
   Medic not started: vigil-medic can read Vigil secrets (privilege-model check 8):
     /home/<you>/vigil/.env
   Make each readable by its owner only, then ./start.sh -d again:
     chmod 0600 /home/<you>/vigil/.env
   ```

   A `.env` copied from `env.example` is usually `0644`, so expect this once.

Medic then runs as `vigil-medic` from `/opt/vigil-medic`, with install shape `start_sh`, and reads the agent worker's readiness at `127.0.0.1:6990`, where `scripts/agent_up.sh` starts it. Its log is `logs/medic.log`, rotated to `medic.log.1` and `medic.log.2` at 10 MiB each, so it never takes more than 30 MiB; `./shutdown_all.sh` stops it.

**Resource budget.** There is no container limit on this shape, so the restart loop applies Medic's 1 vCPU / 1 GiB budget itself. Medic runs under `nice -n 10`, so Vigil wins whenever the CPU is contended (priority only, not a quota). The loop reads Medic's resident memory every 5 seconds; above 1 GiB it stops Medic, logs a line naming the RSS and the limit (`Medic (pid …) uses … MiB RSS, over the 1024 MiB limit; stopping it`), and restarts it with the usual backoff. Repeated stops count toward the same crash-loop cap as any other exit.

**Status in the console.** This shape has no gateway: the backend polls Medic's `GET /v1/status` directly on `127.0.0.1:8470`, where Medic listens (loopback only). Before it starts the backend, `./start.sh -d` sets the two backend settings, unless `.env` already gives them a value:

| Setting | Value on host-native |
|---|---|
| `VIGIL_MEDIC_API_URL` | `http://127.0.0.1:8470` |
| `VIGIL_MEDIC_API_KEY_FILE` | `~/.vigil/medic_api_key` (the State Directory, `VIGIL_DIR`) |

The key (32 random bytes, base64url) is created `0600` on the first start with Medic on and kept across restarts; a key file not in that format is replaced. `vigil-medic` can't read it there, so `start.sh` hands it to Medic's restart loop on stdin, and the loop keeps Medic's own `0600` copy at `<data dir>/run/api_key`. To rotate it, delete `~/.vigil/medic_api_key` and run `./shutdown_all.sh` then `./start.sh -d`. Medic without a usable key keeps watching with its API off (`logs/medic.log` says so), and the console reads Down.

Host-native Medic doesn't use the gateway yet, so no service account is needed. If a host-native gateway is added later, it uses the same `ensure` command with Vigil's venv: `venv/bin/python -m core.auth.service_account ensure <name> < password-file`.
