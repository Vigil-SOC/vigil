"""Compose must re-apply infra/database/init/*.sql after create_all on `up`.

Issue #769: mounting the SQL at /docker-entrypoint-initdb.d lets Postgres run
it with ON_ERROR_STOP=1 before the backend's create_all. 05 is data-only
against ORM tables, so it aborts and 06–23 never run. Roles stay empty and
agent_run_leases is never created. A seed profile is not a fix — this compose
file has no wrapper that `compose run`s it. The desktop stack already does
the right thing: 00_apply.sh (ON_ERROR_STOP=0) plus db-seed waiting on
sla_policies, not /api/health.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
APPLY_SH = REPO / "infra" / "docker" / "initdb" / "00_apply.sh"
INIT_SQL = REPO / "infra" / "database" / "init"


def _services() -> dict:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return (raw or {}).get("services") or {}


def _volume_targets(spec: dict) -> dict[str, str]:
    """Map container path -> host path for bind mounts."""
    out: dict[str, str] = {}
    for entry in spec.get("volumes") or []:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        host, container = entry.split(":", 1)
        container = container.split(":")[0]
        out[container] = host
    return out


def _entrypoint_text(spec: dict) -> str:
    ep = spec.get("entrypoint") or []
    if isinstance(ep, str):
        return ep
    return "\n".join(str(part) for part in ep)


def test_postgres_does_not_feed_sql_to_initdb_entrypoint() -> None:
    # The official image runs *.sql here with ON_ERROR_STOP=1. That was the bug.
    mounts = _volume_targets(_services()["postgres"])
    assert mounts.get("/docker-entrypoint-initdb.d") == "./initdb"
    assert mounts.get("/db-init") == "../database/init"
    assert not list(
        (REPO / "infra" / "docker" / "initdb").glob("*.sql")
    ), "SQL under initdb/ would be run by Postgres with ON_ERROR_STOP=1"


def test_apply_script_tolerates_errors_and_runs_every_file() -> None:
    text = APPLY_SH.read_text(encoding="utf-8")
    assert "ON_ERROR_STOP=0" in text
    # Glob, not a hardcoded 06–21 range: 22 and 23 exist, and more will land.
    assert "/db-init/*.sql" in text
    assert list(INIT_SQL.glob("*.sql")), "no SQL files to apply"


def test_db_seed_runs_on_default_up() -> None:
    spec = _services()["db-seed"]
    assert not spec.get(
        "profiles"
    ), "db-seed is behind a profile, so `docker compose up` will not run it"
    assert (
        spec.get("restart") == "no"
    ), "db-seed must be one-shot; unless-stopped would replay CREATE TRIGGER forever"
    text = _entrypoint_text(spec)
    assert "to_regclass('public.sla_policies')" in text
    assert "/api/health" not in text
    assert "exec sh /apply.sh" in text
    mounts = _volume_targets(spec)
    assert mounts.get("/db-init") == "../database/init"
    assert mounts.get("/apply.sh") == "./initdb/00_apply.sh"
    assert not spec.get(
        "ports"
    ), "db-seed is a one-shot client, not a published service"
    depends = spec.get("depends_on") or {}
    # A postgres-only depends_on starts db-seed before bifrost, so the 60s
    # sla_policies wait can expire before create_all has even begun.
    assert (depends.get("backend") or {}).get("condition") == "service_started"
