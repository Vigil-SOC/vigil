# Database init SQL — source of truth

These `*.sql` files are the canonical schema initialization scripts. The
docker-compose stack reads them directly from this directory; the Helm
chart reads from a **copy** under `helm/vigil/files/database-init/`
(Helm can only load files from inside the chart directory).

## Execution order — two different rules

The two deploy paths order these files differently. When you add a new
file, you need to satisfy both.

- **docker-compose (local dev)** — the postgres image runs every file
  it finds under `/docker-entrypoint-initdb.d` in **lexicographic
  filename order**. So `01_*.sql` runs before `04_*.sql` runs before
  `16_*.sql`. The `NN_` prefix on each filename **is** the ordering
  mechanism here. Pick a prefix that sorts correctly relative to any
  files your script depends on. (Note: zero-padded prefixes like `003_`
  sort *before* `01_` lexicographically.)
- **Helm chart** — the `db-init` Job iterates over
  `helm/vigil/values.yaml`'s `dbInit.sqlFiles` list in the **order
  written there**. Filename prefixes are decorative for this path; the
  list is authoritative. Files in `helm/vigil/files/database-init/`
  that aren't listed are bundled into the ConfigMap but never run.

## When you add a new init SQL file here

You must do all three of the following — CI catches step 1, but **not**
steps 2 and 3:

1. **Copy the file into the chart bundle**:
   ```bash
   cp database/init/NEWFILE.sql helm/vigil/files/database-init/
   ```
   The `Helm Chart / Lint and Template` workflow runs
   `diff -r database/init helm/vigil/files/database-init` on every PR and
   will fail if these two directories drift.

2. **Add the filename to `helm/vigil/values.yaml`** under
   `dbInit.sqlFiles` in the correct position for the Helm execution
   order (see above). Without this step, the chart bundles the file
   into the ConfigMap but the `db-init` Job never runs it — `helm
   install` succeeds and the schema is silently incomplete.

3. **Verify with `helm template`** that the rendered dbInit Job script
   has an `apply` line for your new file. Match the Job script's apply
   line specifically — a bare `grep NEWFILE.sql` will match the
   ConfigMap data key and the SQL file's own header comment too, both
   of which are emitted regardless of whether the file is in
   `dbInit.sqlFiles`, so it will green-light a forgotten step 2:
   ```bash
   helm template release-check helm/vigil \
     --set secrets.anthropicApiKey=test \
     --set secrets.postgresPassword=test \
     | grep -E '^[[:space:]]*apply "NEWFILE\.sql"'
   ```
   No matches → the file is in the ConfigMap but the Job won't run it.

## Reserved filenames — do not use

**Don't name a file `003_add_ai_enrichment.sql` or `003_ai_decision_logs.sql`.**

Earlier versions of `helm/vigil/values.yaml` listed these as ghost
entries in `dbInit.sqlFiles` — files that didn't exist on disk. The
chart's `db-init` Job ran psql against them, got "file not found,"
treated that as a benign warning (it has to, because some real
migrations legitimately fail when SQLAlchemy hasn't created their
target tables yet), then unconditionally inserted the filename into
`_vigil_schema_versions` to mark it "applied." So every v0.1.x Helm
deployment now has rows in that table claiming those two filenames
have been applied.

If a future PR ever ships a real file with one of those exact names,
the Job will check `_vigil_schema_versions`, see the ghost row, and
**SKIP** the file on every pre-existing deployment. The schema change
silently never runs in production.

Pick any other prefix. The rest of this directory uses two-digit
`NN_` (`01_`, `04_`, …, `16_`); follow that convention.

## When you modify an existing init SQL file

Same drill — copy the updated file to `helm/vigil/files/database-init/`
so the chart bundle stays in sync. The `diff -r` lint check will fail
otherwise.

## Why this isn't automated

A pre-commit hook or `make` target that auto-syncs the bundle would
remove the footgun entirely. Filed as a follow-up — until then, the
manual three-step process is what we have.

## See also

- [`helm/vigil/files/database-init/README.md`](../../helm/vigil/files/database-init/README.md)
  — chart-side notes on the same convention.
- [`.github/workflows/helm-chart.yml`](../../.github/workflows/helm-chart.yml)
  — the CI check that enforces directory parity.
