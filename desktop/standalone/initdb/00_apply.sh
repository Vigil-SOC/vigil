#!/bin/sh
# Apply every schema/seed file, tolerating failures.
#
# Postgres' own initdb.d runner uses ON_ERROR_STOP=1, so the first failure
# aborts the whole init and leaves a half-built schema — silently, because the
# container then restarts onto the already-initialized data dir and reports
# healthy. Some files here legitimately fail depending on when they run: the
# data-only ones target tables the backend's create_all builds at startup. The
# Helm db-init Job tolerates failures for the same reason.
#
# Runs twice, from two contexts, which is why it is one script:
#   initdb   — creates extensions and the auth tables (with their DEFAULT
#              clauses, which create_all does not emit) and seeds the admin.
#   db-seed  — after create_all, picks up the data whose tables only exist by
#              then. Every file is idempotent, so the second pass is a no-op
#              where the first already succeeded.
# Connection comes from libpq env: initdb has POSTGRES_* and a local socket,
# db-seed passes PGHOST/PGUSER/PGDATABASE/PGPASSWORD.
: "${PGUSER:=${POSTGRES_USER}}"
: "${PGDATABASE:=${POSTGRES_DB}}"
export PGUSER PGDATABASE

for f in /db-init/*.sql; do
    echo "applying $(basename "$f")"
    psql -v ON_ERROR_STOP=0 -q -f "$f" || true
done
echo "apply complete"
