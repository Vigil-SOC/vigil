# Database init SQL — chart copy

These SQL files are **copies** of `database/init/*.sql` at the repo root.
Helm charts can only read files inside the chart directory, so the init SQL has
to live here for the `db-init` Job's ConfigMap to pick it up.

When a new init SQL file lands under `database/init/`:

1. Copy it here: `cp database/init/NEWFILE.sql helm/vigil/files/database-init/`
2. Add its filename to `values.yaml` under `dbInit.sqlFiles` **in the order
   it should execute** — for the chart path, the list order is authoritative
   and filename prefixes are decorative (this differs from the docker-compose
   path, which runs files lexicographically). Don't pick a filename that
   collides with the reserved-filenames list documented in the source-side
   README.
3. Verify with `helm template ... | grep -E '^[[:space:]]*apply "NEWFILE\.sql"'`
   that the rendered Job script applies your file. A bare `grep NEWFILE.sql`
   gives false positives — the ConfigMap key and the SQL file's own header
   comment both match — so use the tighter pattern.

Two failure modes to know about:

- **Filename listed in `dbInit.sqlFiles` but missing from this directory**
  (e.g. a values.yaml typo or a `cp` that never ran): the `db-init` Job's
  `apply()` function **hard-fails** the install/upgrade with a clear error
  — *unless* the filename already has a row in `_vigil_schema_versions`,
  in which case it's SKIPped as already-applied (so existing v0.1.x
  upgrades don't trip on the historical `003_*` ghost rows). Loud and
  immediate for genuinely new drift; quietly idempotent for the legacy
  ghost-row case.
- **File present in this directory but missing from `dbInit.sqlFiles`**
  (e.g. step 1 done, step 2 forgotten): silent on `helm install` —
  the file ships in the ConfigMap but the Job never applies it. The
  verification grep in step 3 is the only check that catches this.

The CI check at `.github/workflows/helm-chart.yml` runs
`diff -r database/init helm/vigil/files/database-init` and fails on
directory drift between the source and the bundle, but doesn't check
either against `dbInit.sqlFiles`.

See also: [`database/init/README.md`](../../../../database/init/README.md)
for the same convention from the source side.
