# Database init SQL — chart copy

These SQL files are **copies** of `database/init/*.sql` at the repo root.
Helm charts can only read files inside the chart directory, so the init SQL has
to live here for the `db-init` Job's ConfigMap to pick it up.

When a new init SQL file lands under `database/init/`:

1. Copy it here: `cp database/init/NEWFILE.sql helm/vigil/files/database-init/`
2. Add its filename to `values.yaml` under `dbInit.sqlFiles` **in the correct
   execution order** — the ordering is authoritative, not the filename prefix
   (the `003_` collision in the source is a hazard).
3. Verify `helm template` produces a Job script that sources it.

CI check `.github/workflows/helm-chart.yml` will fail if these copies drift
from the source-of-truth files under `database/init/`. Note: that check only
catches directory drift — if you forget step 2 (`values.yaml`), the chart
deploys without your schema change and the failure is silent.

See also: [`database/init/README.md`](../../../../database/init/README.md)
for the same convention from the source side.
