#!/bin/sh
# Set vigil_app's password after python -m core.upgrade has created the role.
# The password is POSTGRES_APP_PASSWORD, never the owner's. App processes log
# in as vigil_app; the owner password stays on the database server and this
# step. :'pw' is interpolated from --set, not -c.
: "${PGUSER:=${POSTGRES_USER}}"
: "${PGDATABASE:=${POSTGRES_DB}}"
export PGUSER PGDATABASE

app_pw="${POSTGRES_APP_PASSWORD:-}"
owner_pw="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
if [ -z "$app_pw" ]; then
    echo "ERROR: POSTGRES_APP_PASSWORD is empty; refusing to give vigil_app the database owner password." >&2
    exit 1
fi
if [ -n "$owner_pw" ] && [ "$app_pw" = "$owner_pw" ]; then
    echo "ERROR: POSTGRES_APP_PASSWORD must differ from the database owner password." >&2
    exit 1
fi
echo "setting vigil_app password"
printf '%s\n' \
    "ALTER ROLE vigil_app PASSWORD :'pw';" \
    "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public.alembic_version FROM vigil_app;" \
    "GRANT SELECT ON TABLE public.alembic_version TO vigil_app;" \
    | psql -v ON_ERROR_STOP=1 --set=pw="$app_pw"
echo "apply complete"
