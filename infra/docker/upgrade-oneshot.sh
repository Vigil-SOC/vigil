#!/bin/sh
# Apply the schema, then set vigil_app's password. A non-zero upgrade exit
# stops here, so the password step does not run and neither does anything
# waiting on this container.
set -eu
dir="${VIGIL_UPGRADE_DIR:?VIGIL_UPGRADE_DIR is required}"
point="$dir/restore-point"
if [ -s "$point" ]; then
    id=$(tr -d '[:space:]' < "$point")
else
    id=""
fi
if [ -n "$id" ]; then
    python -m core.upgrade --restore-point "$id"
else
    python -m core.upgrade
fi
# postgresql-client-16 is installed off PATH. See Dockerfile.backend.
export PATH="/usr/lib/postgresql/16/bin:${PATH}"
export PGHOST="${POSTGRES_HOST:?}"
export PGPORT="${POSTGRES_PORT:-5432}"
export PGUSER="${POSTGRES_USER:?}"
export PGDATABASE="${POSTGRES_DB:?}"
export PGPASSWORD="${POSTGRES_PASSWORD:?}"
exec sh /vigil-app-password.sh
