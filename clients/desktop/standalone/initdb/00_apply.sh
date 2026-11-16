#!/bin/sh
# Set vigil_app's and vigil_agent's passwords after python -m core.upgrade has
# created the roles. vigil_app's is POSTGRES_APP_PASSWORD and vigil_agent's is
# POSTGRES_AGENT_PASSWORD, each distinct and never the owner's. App processes
# log in as vigil_app and the agent processes as vigil_agent; the owner password
# stays on the database server and this step. :'pw' is interpolated from --set,
# not -c.
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
agent_pw="${POSTGRES_AGENT_PASSWORD:-}"
if [ -z "$agent_pw" ]; then
    echo "ERROR: POSTGRES_AGENT_PASSWORD is empty; refusing to set vigil_agent's password." >&2
    exit 1
fi
if [ "$agent_pw" = "$app_pw" ] || { [ -n "$owner_pw" ] && [ "$agent_pw" = "$owner_pw" ]; }; then
    echo "ERROR: POSTGRES_AGENT_PASSWORD must differ from the vigil_app and owner passwords." >&2
    exit 1
fi
echo "setting vigil_app and vigil_agent passwords"
printf '%s\n' \
    "ALTER ROLE vigil_app PASSWORD :'pw';" \
    "ALTER ROLE vigil_agent PASSWORD :'agent_pw';" \
    "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public.alembic_version FROM vigil_app;" \
    "GRANT SELECT ON TABLE public.alembic_version TO vigil_app;" \
    | psql -v ON_ERROR_STOP=1 --set=pw="$app_pw" --set=agent_pw="$agent_pw"
echo "apply complete"
