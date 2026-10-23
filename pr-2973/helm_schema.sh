#!/bin/bash
# Hand replay of ci-cd.yml test-helm-install-schema without helm/yq.
set -euo pipefail
repo=$1; C=pr2892-helm; P=55893
docker rm -f $C >/dev/null 2>&1 || true
docker run -d --rm --name $C -e POSTGRES_DB=deeptempo_soc -e POSTGRES_USER=deeptempo -e POSTGRES_PASSWORD=test -p 127.0.0.1:$P:5432 postgres:16-alpine >/dev/null
for i in $(seq 60); do docker exec $C pg_isready -h 127.0.0.1 -U deeptempo -d deeptempo_soc >/dev/null 2>&1 && break; sleep 1; done; sleep 2
docker cp "$repo/infra/helm/vigil/files/database-init" $C:/sql
files=$(python -c "import yaml;print(' '.join(yaml.safe_load(open('$repo/infra/helm/vigil/values.yaml'))['dbInit']['sqlFiles']))")
echo "== chart SQL as deeptempo"
for f in $files; do docker exec $C psql -q --single-transaction -v ON_ERROR_STOP=1 -U deeptempo -d deeptempo_soc -f /sql/$f >/dev/null 2>&1 || { echo "FAIL $f"; exit 1; }; done
docker exec $C psql -q -U deeptempo -d deeptempo_soc -c "ALTER ROLE vigil_app PASSWORD 'vigil-app-test'"
echo "applied $(echo $files | wc -w) files"
export POSTGRES_HOST=localhost POSTGRES_PORT=$P POSTGRES_DB=deeptempo_soc PYTHONPATH=$repo
cd "$repo"
echo "== create_all + stamp as vigil_app"
POSTGRES_USER=vigil_app POSTGRES_PASSWORD=vigil-app-test python -c "
from core.storage.connection import _stamp_schema_version, get_db_manager
m=get_db_manager(); m.initialize(); m.create_tables(); _stamp_schema_version(m.engine)" 2>&1 | tail -2
echo "== python -m core.upgrade as deeptempo"
POSTGRES_USER=deeptempo POSTGRES_PASSWORD=test VIGIL_UPGRADE_ACK_REASON="CI: fresh install" VIGIL_UPGRADE_ACK_VERSION="$(cat VERSION)" python -m core.upgrade 2>&1 | tail -1 | python -c "import json,sys; r=json.loads(sys.stdin.read()); print('outcome:', r['outcome'], 'to:', r.get('to'), 'drift:', r.get('drift'))"
echo "== strict drift check as vigil_app"
POSTGRES_USER=vigil_app POSTGRES_PASSWORD=vigil-app-test DB_STRICT_SCHEMA=true python -c "
from core.storage.connection import check_schema_drift, get_db_manager, seed_reference_tables
m=get_db_manager(); m.initialize(); m.create_tables(); seed_reference_tables(m); check_schema_drift(m, provisioned=True); print('drift check ok')" 2>&1 | tail -1
docker exec $C psql -tA -U deeptempo -d deeptempo_soc -c "SELECT version_num FROM alembic_version" | sed 's/^/alembic_version: /'
docker exec $C psql -tA -U deeptempo -d deeptempo_soc -c "SELECT count(*) FROM information_schema.columns WHERE table_name='episodic_distil_markers' AND column_name='origin_run_ids'" | sed 's/^/origin_run_ids columns left: /'
docker rm -f $C >/dev/null
