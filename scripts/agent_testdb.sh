#!/usr/bin/env bash
# The Postgres services/agent/tests/integration expects, at the DSN they default to.
# Nothing else stands it up -- not start.sh, not CI -- so those 37 tests silently do
# not run, and the ledger, lease and hunt-run paths go unverified.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=vigil-agent-testdb
PORT=55432

if [ "${1:-up}" = "down" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "removed $NAME"
  exit 0
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" \
    -e POSTGRES_USER=vigil -e POSTGRES_PASSWORD=vigil -e POSTGRES_DB=vigil_test \
    -p "$PORT:5432" pgvector/pgvector:pg16 >/dev/null
fi

until docker exec "$NAME" pg_isready -U vigil -d vigil_test >/dev/null 2>&1; do sleep 1; done

# Only the agent layer's own tables: these tests touch no other schema, and the
# rest of infra/database/init assumes an ordering this does not need.
for sql in 19_agent_ledger 20_agent_directives 21_agent_run_leases; do
  docker exec -i "$NAME" psql -q -U vigil -d vigil_test \
    -c "SET client_min_messages = warning" -f - < "infra/database/init/$sql.sql" >/dev/null
done

echo "postgres://vigil:vigil@localhost:$PORT/vigil_test ready"
