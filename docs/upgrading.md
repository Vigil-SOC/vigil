# Upgrading an existing install

`python -m core.upgrade` is the one thing that changes Vigil's database schema. It runs as the database owner (the `POSTGRES_*` settings, or `--database-url`), holds a lock so only one copy runs at a time, and prints one JSON line.

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

## Helm

The chart runs the command as Jobs of the backend image, logged in as the database owner, with `/var/lib/vigil-upgrade` on a 1Gi PVC that is kept across upgrades and after `helm uninstall`. Helm takes no Vigil backup. Instead, set both acknowledgement values on `helm install` and on every `helm upgrade`:

```bash
helm upgrade vigil infra/helm/vigil -n vigil --reuse-values --timeout 30m \
  --set upgrade.acknowledgeVersion=0.8.0 \
  --set upgrade.acknowledgeReason="RDS snapshot vigil-2026-10-22 taken at 09:00"
```

`upgrade.acknowledgeVersion` must equal the chart's `appVersion`, and the backend image must be that version. A value left over from an earlier upgrade (with `--reuse-values`, the previous one) refuses, as does an empty reason. That way each upgrade is acknowledged separately.

**Install.** A post-install Job takes the empty database to head and sets `vigil_app`'s password. App pods fail their startup check until it finishes, then become Ready. Do not pass `--wait` on the first install. Helm waits for Ready before it runs post-install hooks, so the install would time out.

**Upgrade.** Two pre-upgrade Jobs run before Helm changes any manifest:

1. Weight -10 checks the acknowledgement, then runs `python -m core.upgrade --check`. When it refuses, Helm stops. Nothing has been stopped, and the previous release keeps serving.
2. Weight 0 records the replica count of every Deployment and StatefulSet in the release, and the KEDA pause annotation on their ScaledObjects. Postgres and Redis are left out. It pauses KEDA (`autoscaling.keda.sh/paused-replicas: "0"`) and scales the workloads to 0. That covers the API, daemon, workers, agent processes, MCP runner, Medic and its gateway, pgAdmin and Splunk. An HPA stops acting on a target at 0 replicas, so HPAs are not changed. The Job waits until those pods are gone (`upgrade.stopTimeoutSeconds`), terminates the remaining `vigil_app` sessions in the database, and runs the upgrade. Where the owner is not a superuser (Bitnami, most managed databases), ending another role's session needs `pg_signal_backend`. Without that grant, an upgrade that finds a leftover session refuses and restores. Then Helm applies the new manifests, and the new pods start at head.

From the scale-down until the new pods are Ready, nothing is served. Plan this as a maintenance window and give `--timeout` enough time for it. Source events are not queued. When the release is back, pollers re-read their lookback.

When the upgrade fails:

- **`alembic_version` unchanged.** The Job puts back the replica counts and KEDA annotations it recorded, and exits non-zero. The previous release serves again. A refusal does this, and so does an adoption that rolled back.
- **`alembic_version` changed.** A revision has run, so the workloads stay at 0 and KEDA stays paused. Restore your own backup of the database, then run `helm rollback`. Then remove the pause annotation from each ScaledObject; the Job's log prints the `kubectl annotate` command for each one. `helm rollback` does not remove an annotation it never set.

A 0.7.0 or 0.8.0 Helm database is adopted by the command, as described below. A database with any other stamp refuses at the weight -10 check, and the previous release keeps serving.

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
6. compares the result with the schema revision `0001_baseline` creates. If the two match, it records revision `0001_baseline` in the same transaction. Revisions `0004` and `0007` run after that stamp, as on a fresh install, and add the session-cutoff and conversation-generation columns.

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
