# Upgrading an existing install

`python -m core.upgrade` is the one thing that changes Vigil's database schema. It runs as the database owner (the `POSTGRES_*` settings, or `--database-url`), holds a lock so only one copy runs at a time, and prints one JSON line.

```bash
python -m core.upgrade --check                    # report only; writes nothing
python -m core.upgrade --restore-point <id>       # apply, naming the restore point just taken
```

Instead of a restore point you can set `VIGIL_UPGRADE_ACK_VERSION` to this release's version and `VIGIL_UPGRADE_ACK_REASON` to why you are going ahead without one. Each run that applies or refuses is appended to `$VIGIL_UPGRADE_DIR/history.jsonl`.

## Adopting a 0.7.0 or 0.8.0 database

A database that has Vigil's tables but no `alembic_version` table was installed by 0.7.0 or 0.8.0. It is not rebuilt. In one transaction the command:

1. hands tables that `vigil_app` owns (Helm's `create_all` built some as `vigil_app`) to the owner it connects as;
2. applies the init files Helm never ran (`05`, `28`, `39`, `34_drop_skills`, and `34_mcp_credentials` when that table is missing);
3. revokes leftover grants to the `deeptempo` role (the role itself stays) and deletes the old `admin`, `analyst` and `viewer` roles that no user holds. A role that a user still holds is kept, and the report names it under `adoption.kept_roles`. Move those users to another role when you can;
4. applies the schema changes made since the release it found: from 0.7.0, files `40` to `45`, `mfa_last_totp_step` and the two `findings` noise columns; from 0.8.0, `44` and `45` only;
5. re-applies `30_vigil_app_role.sql`, so `vigil_app` can log in and has grants on every table;
6. compares the result with the schema a fresh install has. If the two match, it records revision `0001_baseline` in the same transaction.

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
