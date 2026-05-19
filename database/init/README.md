# Database init SQL — source of truth

These `*.sql` files are the canonical schema initialization scripts. The
docker-compose stack reads them directly from this directory; the Helm
chart reads from a **copy** under `helm/vigil/files/database-init/`
(Helm can only load files from inside the chart directory).

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
   `dbInit.sqlFiles` in the **correct execution order**. The order in
   that list is authoritative — filename prefixes (`01_`, `02_`, …) are
   a convention, not enforcement. Without this step, the chart deploys
   without your schema change and `helm install` succeeds silently.

3. **Verify with `helm template`** that the rendered dbInit Job script
   includes a `psql` invocation for your new file:
   ```bash
   helm template release-check helm/vigil \
     --set secrets.anthropicApiKey=test \
     --set secrets.postgresPassword=test \
     | grep NEWFILE.sql
   ```

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
