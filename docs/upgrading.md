# Upgrading an existing install

`python -m core.upgrade` is the one thing that changes Vigil's database schema. It runs as the database owner (the `POSTGRES_*` settings, or `--database-url`), holds a lock so only one copy runs at a time, and prints one JSON line.

The app logs in as `vigil_app`, which reads and writes rows and cannot create objects in the `public` schema. Revision `0023_runtime_role_no_create` removes that grant. Anything of your own that creates objects as `vigil_app` now gets `permission denied for schema public`; give it its own role. A `public` schema that still grants `CREATE` to `PUBLIC` (a database first created on PostgreSQL 14 or older) leaves `vigil_app` able to create through `PUBLIC`. `--check` lists that as drift on `schema public`, and `REVOKE CREATE ON SCHEMA public FROM PUBLIC` closes it.

```bash
python -m core.upgrade --check                    # report only; writes nothing
python -m core.upgrade --restore-point <id>       # apply, naming the restore point just taken
```

Instead of a restore point you can set `VIGIL_UPGRADE_ACK_VERSION` to this release's version and `VIGIL_UPGRADE_ACK_REASON` to why you are going ahead without one. Each run that applies or refuses is appended to `$VIGIL_UPGRADE_DIR/history.jsonl`.

## Compose, the desktop app, and start.sh

`docker compose up`, the desktop stack, and `./start.sh` (and `scripts/app_up.sh`) run this command for you. It runs once Postgres is healthy and before the API, the daemon, the workers, and the agent processes start. Nothing is served while it runs: that is a maintenance window. Source events are not queued. When the stack is back, pollers re-read their lookback — 60 minutes for Splunk.

The directory is `/var/lib/vigil-upgrade` in Compose and on the desktop (the `upgrade_state` volume), and `~/.vigil-upgrade` for `start.sh`. A pre-upgrade snapshot is taken first and its id is passed in. Set the two acknowledgement variables above to go ahead without one.

A refused upgrade changes nothing and exits non-zero. This install stays down until you fix the cause or reinstall the previous release. The API, daemon, workers, and agent processes are not started.

Rolling back by image tag alone is not the way back. Restore the restore point, using the release that took it, then install the previous release.

Each snapshot, the pre-upgrade one included, is followed by a second one in the same repository, tagged `wrapping-key`, holding only `JWT_SECRET_KEY`; the snapshot's own `.env` has that line removed. A restore keeps the install's `JWT_SECRET_KEY` and falls back to the paired one if the install's does not open the restored key ring (see [compromise recovery](compromise-recovery.md)). Snapshots from before this have no pair and restore as before.

## Helm

The chart runs the command as Jobs of the backend image, logged in as the database owner, with `/var/lib/vigil-upgrade` on a 1Gi PVC that is kept across upgrades and after `helm uninstall`. Helm takes no Vigil backup. Instead, set both acknowledgement values on `helm install` and on every `helm upgrade`:

```bash
helm upgrade vigil infra/helm/vigil -n vigil --reuse-values --timeout 30m \
  --set upgrade.acknowledgeVersion=0.8.0 \
  --set upgrade.acknowledgeReason="RDS snapshot vigil-2026-10-22 taken at 09:00"
```

`upgrade.acknowledgeVersion` must equal the chart's `appVersion`, and the backend image must be that version. A value left over from an earlier upgrade (with `--reuse-values`, the previous one) refuses, as does an empty reason. That way each upgrade is acknowledged separately.

**Install.** A post-install Job takes the empty database to head and sets `vigil_app`'s and `vigil_agent`'s passwords. App pods fail their startup check until it finishes, then become Ready. Do not pass `--wait` on the first install. Helm waits for Ready before it runs post-install hooks, so the install would time out.

**Upgrade.** Two pre-upgrade Jobs run before Helm changes any manifest:

1. Weight -10 checks the acknowledgement, then runs `python -m core.upgrade --check`. When it refuses, Helm stops. Nothing has been stopped, and the previous release keeps serving.
2. Weight 0 records the replica count of every Deployment and StatefulSet in the release, and the KEDA pause annotation on their ScaledObjects. Postgres and Redis are left out. It pauses KEDA (`autoscaling.keda.sh/paused-replicas: "0"`) and scales the workloads to 0. That covers the API, daemon, workers, agent processes, MCP runner, Medic and its gateway, pgAdmin and Splunk. An HPA stops acting on a target at 0 replicas, so HPAs are not changed. The Job waits until those pods are gone (`upgrade.stopTimeoutSeconds`), terminates the remaining `vigil_app` sessions in the database, and runs the upgrade. Where the owner is not a superuser (Bitnami, most managed databases), ending another role's session needs `pg_signal_backend`. Without that grant, an upgrade that finds a leftover session refuses and restores. Then Helm applies the new manifests, and the new pods start at head.

From the scale-down until the new pods are Ready, nothing is served. Plan this as a maintenance window and give `--timeout` enough time for it. Source events are not queued. When the release is back, pollers re-read their lookback.

When the upgrade fails:

- **`alembic_version` unchanged.** The Job puts back the replica counts and KEDA annotations it recorded, and exits non-zero. The previous release serves again. A refusal does this, and so does an adoption that rolled back.
- **`alembic_version` changed.** A revision has run, so the workloads stay at 0 and KEDA stays paused. Restore your own backup of the database, then run `helm rollback`. Then remove the pause annotation from each ScaledObject; the Job's log prints the `kubectl annotate` command for each one. `helm rollback` does not remove an annotation it never set.

A 0.7.0 or 0.8.0 Helm database is adopted by the command, as described below. A database with any other stamp refuses at the weight -10 check, and the previous release keeps serving.

Rotating `AGENT_INTERNAL_TOKEN` is not an upgrade step. Finish that rotation, or undo it completely, before an upgrade or a rollback. A rollback of a chart-rendered Secret restores that revision's value. The procedure is [Rotating AGENT_INTERNAL_TOKEN](agent-internal-token.md).

A rollback restores the pre-upgrade database, so it loses the reservations of erased people's usernames and emails written since. Vigil's restore re-applies the erasures themselves and reserves those names again from the erasure list.

**Check only.** To see what an upgrade would do, run `--check` once as a Job of the new backend image, logged in as the owner. Do not use `helm upgrade` for this. Without the acknowledgement, a pending upgrade answers `needs_restore_point` and exits 1. With it, the answer is `apply`. For the in-chart Postgres, with release `vigil` in namespace `vigil`:

```bash
kubectl -n vigil apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: vigil-upgrade-check-only
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      # The upgrade Jobs' labels, so their NetworkPolicies admit this pod.
      labels:
        app.kubernetes.io/name: vigil-upgrade
        app.kubernetes.io/instance: vigil
        app.kubernetes.io/component: upgrade
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      containers:
        - name: check
          image: ghcr.io/vigil-soc/vigil-backend:0.8.0   # the release you plan to install
          command: ["python", "-m", "core.upgrade", "--check"]
          env:
            - {name: POSTGRES_HOST, value: vigil-postgres}
            - {name: POSTGRES_DB, value: deeptempo_soc}
            - {name: POSTGRES_USER, value: deeptempo}   # the owner, not vigil_app
            - name: POSTGRES_PASSWORD
              valueFrom: {secretKeyRef: {name: vigil-postgres, key: POSTGRES_PASSWORD}}
            - {name: VIGIL_UPGRADE_ACK_VERSION, value: "0.8.0"}
            - {name: VIGIL_UPGRADE_ACK_REASON, value: "check only"}
EOF
# Exit 0 or 1 both carry the report: wait for either.
until kubectl -n vigil get job/vigil-upgrade-check-only \
    -o jsonpath='{.status.succeeded}{.status.failed}' | grep -q 1; do sleep 2; done
kubectl -n vigil logs job/vigil-upgrade-check-only
```

## Adopting a 0.7.0 or 0.8.0 database

A database that has Vigil's tables but no `alembic_version` table was installed by 0.7.0 or 0.8.0. It is not rebuilt. In one transaction the command:

1. hands tables that `vigil_app` owns (Helm's `create_all` built some as `vigil_app`) to the owner it connects as;
2. applies the init files Helm never ran (`05`, `28`, `39`, `34_drop_skills`, and `34_mcp_credentials` when that table is missing);
3. revokes leftover grants to the `deeptempo` role (the role itself stays) and deletes the old `admin`, `analyst` and `viewer` roles that no user holds. A role that a user still holds is kept, and the report names it under `adoption.kept_roles`. Move those users to another role when you can;
4. applies the schema changes made since the release it found: from 0.7.0, files `40` to `45`, `mfa_last_totp_step` and the two `findings` noise columns; from 0.8.0, `mfa_last_totp_step`, the two `findings` noise columns, `44` and `45`;
5. re-applies `30_vigil_app_role.sql`, so `vigil_app` can log in and has grants on every table;
6. compares the result with the schema revision `0001_baseline` creates. If the two match, it records revision `0001_baseline` in the same transaction. Revisions `0004` and `0007` run after that stamp, as on a fresh install, and add the session-cutoff and conversation-generation columns. Revision `0061` then drops the session-cutoff column (`users.tokens_valid_after`) again: nothing has read it since the sign-in row became the revocation record, and fresh installs and adopted databases both reach head without it.

The command reads `system_config.schema_version` but never writes or deletes it. `--check` runs the same transaction and then rolls it back. For a database it can adopt, `--check` answers `needs_restore_point` and its reason says that adoption would run, so the pre-upgrade backup is taken as for any other upgrade.

## When it refuses

A refusal exits 1, changes nothing, and gives its reason in the `reason` field.

**Unknown drift.** After the repairs, something Vigil owns still differs from a fresh install. That could be a column, index, constraint, grant or owner on one of Vigil's tables, or a table, function or extension that a fresh install has. The `drift` field lists each object together with what was expected and what was found. It contains schema only, never row data. This refusal has **no override**. A restore point does not get past it and neither does the acknowledgement. Objects that Vigil does not own, such as a table you added or Helm's `_vigil_schema_versions`, are listed under `adoption.unowned` and never block.

**"install a 0.7.x release first".** The database has tables, but `schema_version` is missing, or is not `0.7.0` or `0.8.0`. A pre-0.7 install gives this, and so does a development build that stamped some other version. Install a 0.7.x release, start it once so it stamps the database, then run this upgrade again.

**"restore first".** The database had already reached `0001_baseline`. Then an older release (0.8 or earlier) started against it and its old init path re-stamped `schema_version`. From the baseline on, Vigil no longer writes that row. That is what happens if you roll back by image tag alone. The schema can no longer be trusted. Restore the restore point taken before the upgrade, using the release that took it, and then upgrade again. Do not roll back by changing the image tag.

## Support path

If the drift report names something you did not change, or a change you need to keep, keep running your current release. Then open an issue at <https://github.com/Vigil-SOC/vigil/issues> and attach the `--check` JSON, which holds no row data. Each case that gets reported becomes a known repair in a later release.

## How long adoption is supported

Adoption from 0.7.0 and 0.8.0 is supported until the first release that ships at least six months after the release that introduced `0001_baseline`. From that release on, an install still on 0.7.0 or 0.8.0 must first upgrade through a release that still adopts.

## Email addresses already on file

Revision `0077_email_confirmed` marks every account's email confirmed (`users.is_verified = true`). An address already on the account when you upgrade can be used for a password reset straight away. After the upgrade, only a person changing their own email leaves the new address unconfirmed, until they open the 24-hour link mailed to it. A new account's email, and an email an identity provider sets, counts as confirmed and is not sent that link.

## Idle limits of 0 to 12 hours

Both idle limits accept 0 (no idle limit) through 720 minutes (12 hours), set independently of each other. The defaults stay 15 minutes for a standard sign-in and 10 for a privileged one. A value already saved inside the previous ranges (standard 1–60, privileged 1–30, and the privileged limit not above the standard one) behaves the same after the upgrade. Nothing is migrated, and there is no second reader.

A privileged idle of 0 does not turn off Confirm it's you. That window stays 10 minutes. A nonzero privileged idle is the window, up to 12 hours.

**Rolling back** to the previous release after saving an idle of 0, a value above the previous range, or a privileged limit above the standard one makes the stored policy unreadable until an administrator saves Settings > Sessions again. That is what an unreadable policy already does:

- Sessions shows the defaults and says the stored policy could not be read.
- Token mint and rotate are refused.
- The unused-token sweep stops: no reminder and no revocation.
- The Day 1 session checks read "Could not read the session settings."

Saving Settings > Sessions again writes a policy the previous release can read, and those stop.

## What the sign-in changes do on upgrade day

An upgrade from a database older than these changes does the following. Everything is read from the revisions and the sign-in code.

**Every sign-in ends once.** Revision `0042_server_enforced_sign_ins` ends every open sign-in with the reason `upgrade`, because the tokens already issued carry no refresh generation or stored browser. Everyone signs in again, once. A service account is no exception: its open sign-in ends, its password is unchanged, and whatever uses it has to sign in again. A shift change is a good time to upgrade.

**Dormancy clocks start at the upgrade.** Revision `0048_dormancy` sets every account's `dormancy_floor` to the time it ran. An account's idle time is counted from the latest of its last sign-in, its credentials' last use, its creation and that floor, so nobody is deactivated for time that passed before the rule existed. The first account can be deactivated once the session policy's `dormancy_days` (default 35) have passed since the upgrade. `null` turns the sweep off.

**Idle API tokens are reminded, then revoked (Compose and Helm).** Revision `0092_token_reminded_at` adds `mcp_credentials.reminded_at`. The session policy's `token_unused_days` (default 45, 7 to 365, `null` is off) turns the rule on for every install. A token's idle time is counted from its last use, or its creation if it was never used. The first hourly pass after the upgrade reminds each token already idle past the period, once, and tells its holder (for a service account, its accountable person or the account managers). The token is revoked no sooner than 7 days after that reminder, unless it is used first. The upgrade report counts those tokens (`tokens_to_remind`), so it is the number to expect. Directory sync's token is exempt while directory sync is on. On Compose and on Helm alike, every API replica runs the pass, and one reminder and one revocation result. Use a token that must be kept before the 7 days pass, or raise the period in Settings > Sessions (a person's act).

**Single sign-on meets Require MFA only on the provider's word (Compose and Helm).** Revision `0097_sso_mfa_met` adds `sign_ins.mfa_met` and `sign_ins.ends_at`. From the upgrade on, Require MFA and Require phishing-resistant MFA mean the same for a sign-in through the identity provider as for a password sign-in. Open sign-ins are not ended: each runs to its usual end, and the rule meets the person at their next sign-in. At that sign-in, Vigil reads the provider's verified ID token. `amr` containing `mfa`, or `acr` of `phr` or `phrh`, meets Require MFA; only `acr` `phr` or `phrh` meets Require phishing-resistant MFA. Each counts only with an `auth_time` inside the absolute sign-in length (12 hours by default), and such a sign-in ends that long after `auth_time`. A provider's own labels don't count. Otherwise a person who holds a Vigil factor is asked for it after the provider, and a covered person without one is held at "Set up two-step sign-in to continue" and sets one up there by confirming at the provider. Vigil asks every provider for `amr`, `acr` and `auth_time` and sends `max_age`; if the provider leaves them out of the ID token, add them for Vigil's client there (a claim mapper or the token configuration). Each sign-in, and its `auth.sign_in` entry, records how MFA was met (`provider_mfa`, `provider_phr`, `vigil_factor`, `step_down` or `none`); `amr` and `acr` stay on the entry as before. The upgrade report counts the active covered people who sign in only through the provider: `sso_only_with_factor` will be asked for their factor, and `sso_only_without_factor` will be held at setup unless their provider sends a qualifying claim.

**The MFA counts include people who sign in only through the provider.** The daily `auth.mfa_coverage_gap` entry's `count`, the bell's line and the Sessions confirms now count them: such a person has MFA only with a Vigil factor or when their latest SSO sign-in recorded `provider_mfa`, `provider_phr` or `step_down`, and a security key only with a Vigil key or `provider_phr`; one with no SSO sign-in since the upgrade counts as without (#4758).

**Failure counts reset.** Revision `0047_lockout_policy` sets every account's failed-login count to 0. Locks are the session policy's now (`auth.session_policy`).

**A saved lockout count outside 3–10 moves to the nearest bound.** Revision `0079_lockout_threshold` sets a saved `lockout_threshold` of 1 or 2 to 3, and of 11 through 20 to 10, and increments that policy's version. `updated_by` is left as it was. The audit trail records the move once, as the upgrade's act (`session_policy.changed`, actor `system`), with the old and the new value. A value already from 3 to 10, no saved policy, and a value outside 1–20 stay as they are, and no entry is written. From then on the setting is 3–10.

**The first sign-in of each person, in order:**

1. The password is checked. A lock refuses it even when it is right. A good password stored as a bcrypt hash is replaced by a PBKDF2 hash at once; the password itself and `password_changed_at` do not change.
2. If the account has two-step sign-in, the code or key is checked. An old recovery code (from before the new hashing) is accepted once. The whole old set is then voided and a new set is shown once, so the person must keep those. A person who signed in with the app code gets the new set the same way, as their stored set is from the old scheme.
3. The password is checked against the breached-password list and the policy: a listed password, one past its maximum age, or one shorter than the lower of 15 and the minimum length without two-step sign-in is not accepted for a sign-in. The answer is `password_change_required` with a reset link, and the person chooses a new password. A spent code stays spent.
4. The sign-in is created. If the account is already at the sign-in limit, its oldest open sign-in ends.

**Settings that left.** These are fixed and ignored when set; the API logs one line per variable at start: `JWT_ACCESS_EXPIRATION_MINUTES` (30 minutes), `JWT_REFRESH_EXPIRATION_DAYS` (7 days), `PASSWORD_RESET_TTL_SECONDS` (3600 seconds). `AUTH_MIN_ZXCVBN_SCORE` is ignored too: it is not fixed at 3, and the strength score is set in Settings › Sessions (default 3). `AUTH_LOCKOUT_THRESHOLD`, `AUTH_LOCKOUT_DURATION_MINUTES` (now the session policy's lockout fields) and `REVOCATION_FAIL_OPEN` (Redis no longer holds revocation state) are also ignored. Remove all of them from a copied `.env` and from Helm values.

**Password hashes.** Revision `0040_password_scheme_stamp` writes the upgrade time once, to `system_config` key `auth.password_scheme_upgraded_at`; a re-run never overwrites it. Read it with:

```sql
SELECT value #>> '{}' AS password_scheme_upgraded_at
FROM system_config WHERE key = 'auth.password_scheme_upgraded_at';
```

Each bcrypt password hash is replaced at that person's first good sign-in. The old hashing ends 12 months after the date in that row. A person who has not signed in by then sets a new password with a reset link. To find accounts that have not signed in, open Settings > Users: the Last Login column shows each account's last sign-in, and "Never" for an account that has none.

## Password minimum and strength score

The minimum length is 3–255 characters (default 16) and the strength score is a setting, 0–4 (default 3). At 0, no score is required. The minimum no longer depends on Require MFA. A policy saved before this release has no score and reads as 3, so an install that never changed a setting behaves the same. Set both in Settings › Sessions.

A sign-in without MFA still rechecks a password shorter than the lower of 15 and the minimum. Raising the minimum sends a person without MFA whose password is under that length to a forced change at the next sign-in. A password on the breached list, and one past its maximum age, are still rechecked. An imported breached list keeps entries down to 3 characters; a list imported before this release covers short entries once it is imported again. The shipped list holds no entry under 12 characters.

An install with no saved policy that still sets `AUTH_MIN_PASSWORD_LENGTH` carries that length once, clamped into 3–255, where the variable reaches the API process. A value of exactly 12 counts as unset, so the defaults apply. `AUTH_MIN_ZXCVBN_SCORE` is not carried into the score.

**Rolling back** to the previous release has costs after either of these:

- saving a length outside 12–64, or any other value the previous release's ranges refuse;
- saving a minimum under 15, or seeding one from the host, while Require MFA is not everyone (that release refuses the combination).

Until an administrator saves Settings › Sessions again, the previous release:

- reads the policy as unreadable, enforces its own defaults (a 3-character minimum reads as 16) and says so on Sessions;
- refuses token mint and rotate, and stops the unused-token sweep;
- shows "Could not read" on the Day 1 session checks.

Also on the previous release:

- it ignores the new strength field and applies 3 silently, and a save made there drops the field;
- a person without MFA whose password is under 15 characters is sent to a forced change at sign-in;
- a password over 128 characters is a wrong password, and the way back is a reset link to a password of 128 or fewer.

## BIFROST_URL and OLLAMA_URL in .env

`env.example` no longer sets `BIFROST_URL` or `OLLAMA_URL` (#5200). Each install shape now uses its own default: host-run uses localhost, Compose uses `http://bifrost:8080` and `http://host.docker.internal:11434`, and Helm sets its own. An existing `.env` is not rewritten. On a full-stack Compose install, delete the shipped `BIFROST_URL="http://localhost:8080"` and `OLLAMA_URL="http://localhost:11434"` lines from `.env`: a value there overrides the Compose defaults, and inside a container localhost is the container itself, so model calls never reach the gateway or Ollama. A host-run install may keep them.

## Medic's gateway token on Compose

Medic's gateway on Compose reads the backend with an API token now, in a `medic_gateway_token` file beside `viewer_password` (#4429, epic #4425). A site enabled before keeps reading on its password with no step on upgrade day: both files are optional, and `./start.sh` creates an empty placeholder for a missing one, which the gateway treats as absent (a `docker compose` command typed by hand doesn't; see `docs/medic/enable.md`). To move the site, re-run `scripts/medic/enable-compose.sh`; it mints the token and recreates the gateway, and the backend ends the account's password when it first accepts the token. Nothing moves a site on its own. The password path is kept through an announced window and is removed no earlier than the release that ends it. After restoring a database from before the mint, mint again: delete `medic_gateway_token` and re-run the script (see `docs/medic/enable.md`).

## What run scoping does to unfinished runs on upgrade day

Run scoping refuses an agent run's calls unless the run's own principal (the credential Vigil signs for it when it starts) names that run and has not expired. Revision `0065_end_unscoped_runs` ends, at the upgrade, every started run that could not make its next call under that rule.

**Which runs it ends.** Each run whose console row is still `running` or `paused` and which has started (its ledger in `agent_events` holds its `run` event) is judged by the principal stored on that event. The run is ended when that principal is missing, can't be read, names another run, or has expired. The claims are read without checking the signature: the Helm upgrade Jobs have no app Secret, and reading only ever ends a run. Each ended run:

- shows as failed in the console, with the reason `started before run scoping; run it again`;
- gets a ledger end with outcome `failed` and the same reason, unless it already has one;
- has its pending approvals rejected by `system` with that reason, each recorded on the case trail.

A run ended this way that had been running for longer than about 24 days gets no duration.

A run whose principal names it and is still live is left alone and resumes after the upgrade. So does a run queued but not yet started (it has no ledger yet), and a child run, which has no console row and follows its parent. The upgrade's history record counts the runs it ended under `reports`, as `{"revision": "0065_end_unscoped_runs", "report": {"ended_runs": N}}`. Running the upgrade again ends nothing more.

**A run outlives its credential at 30 days.** A run's principal lasts 31 days from its start, and a run may park for up to 30 days at a time, so a run still unfinished after about 30 days is near or past its credential's end. In practice the parked runs this ends are those started more than 31 days ago, plus runs from older releases on installs that skip releases.

**Before you upgrade.** To keep this from ending a run, let it finish or cancel it in the console, and let the job queue drain: stop starting runs, and wait until the workers have picked up everything queued.

**Installs that skip releases** need no extra step: the revision runs on whichever upgrade first includes it. A job still in the queue without a valid principal ends at its first pickup by the worker, but its console row stays `running` until a person cancels it.

**Rolling back.**

- Compose, desktop and `start.sh` roll back by Vigil's restore, which already stops every run (#4272) and discards agent runs still queued.
- Helm rolls back by restoring the operator's own database backup and running `helm rollback` (see [Helm](#helm)). Vigil's run-stopping restore doesn't run, and the restored runs carry on under the older release, which doesn't check principals.

## Restricted cases and restores

A restricted case's reader list is part of the case row, so a restore brings back the list as it was when the backup was taken. **Restoring a backup taken before a case was restricted reopens that case** to everyone who can read cases. Revisions run forward only (`core/upgrade/__init__.py`), so this holds for Vigil's restore and for a Helm rollback from your own backup alike. After a restore, restrict again any case that must stay restricted; the audit trail's `case.restrict` entries name them.

## What teams add to the case API

Teams (1.1.2, epic #4306) keep the case API's routes and tools and add two refusals to them. Nothing is migrated, and a caller that sets no owner and merges within one team sees no change.

**A newly set owner must hold `cases.write` and be on the case's team.** `PATCH /api/v1/cases/{id}` and the frozen `create_case` and `update_case` tools (the agent tools and the MCP ones) check the owner a save newly gives the case. The checks run in this order:

1. No such user, or a user without `cases.write`: 400 `OWNER_CANNOT_WRITE`.
2. Not a current member of the case's team: 400 `OWNER_OFF_TEAM`. An inactive person whose membership row is still there is not current. The message names `POST /api/v1/cases/{id}/move` and the `move_case` tool, which can carry the owner.

The tools answer `{"error", "code"}` with the same codes instead of an HTTP status. `create_case` judges the default team, and its `OWNER_OFF_TEAM` message holds the literal `{case_id}`, as the case does not exist yet; the route to use is `POST /api/v1/cases/{id}/move` with the real id. Saving the stored owner again, and clearing it, are not checked. `OWNER_CANNOT_WRITE` applies from the upgrade, while everyone is on the default team. `OWNER_OFF_TEAM` applies only once an admin splits teams.

**A cross-team merge needs the source team or `cases.move`.** `POST /api/v1/cases/{id}/merge` (`{id}` is the target; the body's `source_case_id` is the source) is checked only when the two cases are on different teams: the caller must be a member of the source case's team or hold `cases.move`, or the answer is 403 with `Permission denied: cases.move required`. A merge between two cases on one team is unchanged. There is no frozen MCP merge tool. This applies only once teams are split.

## Model closes follow the autonomy setting

From this release every close a model chooses passes the autonomy setting (#4819), the same check an investigation's close already made. That covers chat, `/mcp` clients (a person's or a service account's), agent tools and runs: `update_case` with `status: "closed"`, `create_case` with `status: "closed"`, and the MCP `close_case`. A close passes only where the run's workflow may act on its own, or, with no workflow (chat, `/mcp`), where the fleet's Act on reversible actions is on.

That switch is off by default, so after the upgrade these closes are left open for a person. The tool answers its usual `{"error": ...}` with the reason, writes nothing to the case but a "Left open for a person" activity, and the run carries on; `create_case` creates nothing. Turn on Act on reversible actions, or set a workflow to Act, to let them close. A ceiling that can't be read holds the close.

REST closes are unchanged: `POST /api/v1/cases/{id}/close`, a `PATCH` to status closed and a merge close as before, whichever token or service account sends them. A case created closed by a tool now also gets a closure row, with category `unspecified`.

The `case.close` audit-trail entry gains `category` and `closed_as`: `model` for a close that passed this check, `person` for a console close, `api` for everything else. The closure row's channel is unchanged, so nothing is migrated.

## The confidence threshold is retired

`respond.confidence_threshold` and `DAEMON_CONFIDENCE_THRESHOLD` released no approval since #2918, and from this release nothing reads them (#5279).

- **The variable** is ignored. An `.env`, Compose override or Helm `config:` value that still sets it starts as before; remove it when convenient.
- **A declared `INTENT.md`** (`VIGIL_INTENT_DIR`) that still names `respond.confidence_threshold` is not rejected: its other keys apply, and the daemon logs one warning naming the retired key. An applied revision that already recorded the key keeps working; the next revision that changes the file drops it from the stored row. Delete the key from the file to stop the warning.
- **Agent prompts** no longer carry an auto-approve band. An operator's stored agent whose system prompt still names `$auto_approve` renders it as `never`; edit the prompt to drop the line.
- **`python -m services.daemon.intent --replay`** (and `--since`) is gone, and passing it is a usage error. Without flags the command still prints declared values beside effective ones.

## An approval's confidence is its step score

From this release `confidence` on an approval, in `/api/v1` and in `/mcp`, carries the step score an investigation filed with its containment (`parameters.step_score.value`, a number from 0 to 1), or null (#5278). It used to carry the proposer's own claim, which is still in the `approval_actions.confidence` column but is no longer read or written. Rows filed before the upgrade, and rows no investigation filed (workflow checkpoints, rule deploys, undos, `create_approval_action`), read null. A score that is unknown also reads null; `parameters.step_score.band` tells it apart (`unknown`), and band `none` means every factor was switched off. A client that compared `confidence` with a threshold should treat null as "no score", not 0.

`create_approval_action` no longer takes `confidence`; one a caller still sends is ignored, and the action files. An agent you wrote that asks for it in its prompt can drop that line.

## One image registry for the Helm chart

From this release `global.imageRegistry` moves every image the chart renders, not only Vigil's own: Postgres, Redis, Bifrost, pgAdmin, Splunk, the `helm test` pod, and the Bitnami and OpenTelemetry subcharts. Its default is now empty. Unset, Vigil's images still come from `ghcr.io/vigil-soc/vigil-<component>` and the others from Docker Hub.

**Values that carry the old default `global.imageRegistry: ghcr.io` must drop it.** Left in, it moves Postgres, Redis and Bifrost to `ghcr.io/library/postgres` and the like, which do not exist, and those pods cannot pull.

**An install that sets `global.imageRegistry` must mirror the third-party images first**, at these paths, with Docker Hub's implied `library/` written out:

- `<registry>/library/postgres:16-alpine` and `<registry>/library/redis:7-alpine`
- `<registry>/maximhq/bifrost:v2.2.1`
- `<registry>/dpage/pgadmin4` and `<registry>/splunk/splunk`, if enabled
- `<registry>/otel/opentelemetry-collector-contrib`, if `otelCollector.enabled`

Or set that component's `image.repository` to a full reference; it is used as written (the Bitnami and OpenTelemetry subcharts still put the registry in front of theirs). Until then those pods cannot pull.

Postgres, Redis and Bifrost are pinned by digest, the same references as both Compose files, so the upgrade restarts their StatefulSets on the pinned image. Nodes that can't reach Docker Hub need it loaded or mirrored first. The digest is part of the default tag (`16-alpine@sha256:…`), so a mirror must keep it: copy the multi-arch index (`docker buildx imagetools create` or `crane copy`), not one platform. A tag you set replaces the pin, and `image.digest`, if set, replaces the tag.

`helm test` now runs from the backend image, which ships curl. `tests.image` is gone and `curlimages/curl` is no longer pulled; remove `tests:` from your values.

## Running with no route out

**The gateway's sheets.** At boot Bifrost fetches its pricing and model-parameter sheets from `getbifrost.ai`, and with no route there it exits. Set `BIFROST_LOCAL_SHEETS=true` in `.env` (Compose) or `bifrost.localSheets: true` (Helm) to read the empty sheets shipped beside `infra/docker/bifrost/config.json` instead. Left unset, the publisher's sheets are used, as before. Turning it off again on an existing gateway volume brings the publisher's pricing back, but Bifrost 2.2.1 keeps the local model-parameter sheet it stored until it is changed in the gateway's own console; Vigil's rate reads do not use that sheet. Bifrost's MCP catalogue sync is off on every install; Vigil does not use it.

With empty sheets the gateway knows no rates, so a hosted model's calls are recorded unpriced, and a run under a cost ceiling (`max_cost_usd`) is parked once too many of its calls are unpriced. A model server inside your site prices at zero instead:

- Ollama always does.
- Any other provider (an OpenAI-compatible server, a LAN gateway) does once it is stated self-hosted: Settings > AI models, the provider's card, Statements, Self-hosted: "Yes, on hardware we control". That writes `config.self_hosted: true` on every active row of the provider type, and the type prices at zero only when every active row carries it. Through the API, `PUT /api/llm/providers/{provider_id}` with `"statements": {"self_hosted": "yes"}` does the same.

A hosted model you still reach through a proxy can be priced by hand in the gateway's pricing overrides.

**OCR.** The backend image carries Tesseract's English data (`eng.traineddata`) in `/usr/share/vigil/tessdata`, which `TESSDATA_PREFIX` names, so a scanned English page is read without a download. Other languages are still fetched on first use; with no route out, put their `.traineddata` files in a folder and point `TESSDATA_PREFIX` at it. That folder must hold `eng.traineddata` too.

**Rule libraries.** A library imported from a repo cannot update without a route to its host, and says so. To keep those rules current, copy them onto the server and add them as a local source under a root listed in `VIGIL_DETECTION_LOCAL_ROOTS`.
