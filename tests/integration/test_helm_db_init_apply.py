"""Helm's db-init Job records a file only when all of it applied, and fails otherwise.

Issue #2578. Runs the Job's own apply() shell function, cut from the chart
template, with psql against a scratch database. The listed files are applied as
a non-superuser database owner with CREATEROLE, as on managed Postgres; the
Helm Install Schema CI job only runs the Job as the image's superuser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "infra" / "helm" / "vigil"
JOB = CHART / "templates" / "db-init-job.yaml"
BUNDLE = CHART / "files" / "database-init"


def _parts():
    return {
        "user": os.getenv("POSTGRES_USER", "deeptempo"),
        "password": os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
    }


def _admin():
    p = _parts()
    return create_engine(
        f"postgresql+psycopg2://{quote(p['user'], safe='')}:{quote(p['password'], safe='')}"
        f"@{p['host']}:{p['port']}/postgres",
        isolation_level="AUTOCOMMIT",
    )


def _postgres_available() -> bool:
    try:
        with _admin().connect():
            return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        shutil.which("psql") is None or not _postgres_available(),
        reason="requires psql and a local PostgreSQL (docker compose up -d postgres)",
    )
)


def _apply_script(sql_dir: Path) -> str:
    """The Job's marker-table setup and apply(), reading files from sql_dir."""
    body = JOB.read_text(encoding="utf-8")
    start = body.index("              psql -v ON_ERROR_STOP=1 -c \"CREATE TABLE IF NOT EXISTS _vigil_schema_versions")
    end = body.index("\n              }\n", start) + len("\n              }\n")
    chunk = textwrap.dedent(body[start:end]).replace('"/sql/$f"', f'"{sql_dir}/$f"')
    assert "{{" not in chunk, "apply() now uses template syntax; render it instead"
    return f'set -eu\n{chunk}\nfor f in "$@"; do apply "$f"; done\n'


@pytest.fixture
def owner_db():
    """A scratch database owned by a non-superuser with CREATEROLE."""
    admin = _admin()
    suffix = uuid.uuid4().hex[:8]
    role, db, pw = f"vigil_dbinit_{suffix}", f"vigil_test_dbinit_{suffix}", "owner-pw"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN CREATEROLE NOSUPERUSER PASSWORD '{pw}'"))
        conn.execute(text(f"CREATE DATABASE {db} OWNER {role}"))
    p = _parts()
    env = {
        **os.environ,
        "PGHOST": p["host"],
        "PGPORT": p["port"],
        "PGDATABASE": db,
        "PGUSER": role,
        "PGPASSWORD": pw,
    }
    try:
        yield env
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)"))
            conn.execute(text(f"REASSIGN OWNED BY {role} TO CURRENT_USER"))
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        admin.dispose()


def _run(env, sql_dir: Path, *files: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", _apply_script(sql_dir), "apply", *files],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _query(env, sql: str) -> str:
    return subprocess.run(
        ["psql", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_every_listed_file_applies_as_a_non_superuser_owner(owner_db) -> None:
    files = yaml.safe_load((CHART / "values.yaml").read_text())["dbInit"]["sqlFiles"]
    result = _run(owner_db, BUNDLE, *files)
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(_query(owner_db, "SELECT count(*) FROM _vigil_schema_versions")) == len(files)
    assert _query(
        owner_db,
        "SELECT rolsuper::text || rolcanlogin::text FROM pg_roles WHERE rolname = 'vigil_app'",
    ) == "falsetrue"


def test_failed_file_is_rolled_back_unrecorded_and_retried(owner_db, tmp_path) -> None:
    sql = tmp_path / "99_broken.sql"
    sql.write_text(
        "CREATE TABLE partial_apply (id int);\nSELECT * FROM no_such_table;\n"
    )
    failed = _run(owner_db, tmp_path, sql.name)
    assert failed.returncode != 0
    assert "99_broken.sql" in failed.stderr and "no_such_table" in failed.stderr
    assert _query(owner_db, "SELECT count(*) FROM _vigil_schema_versions") == "0"
    assert _query(owner_db, "SELECT to_regclass('partial_apply') IS NULL") == "t"

    sql.write_text("CREATE TABLE partial_apply (id int);\n")
    fixed = _run(owner_db, tmp_path, sql.name)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert _query(
        owner_db, "SELECT filename FROM _vigil_schema_versions"
    ) == "99_broken.sql"
    assert _query(owner_db, "SELECT to_regclass('partial_apply') IS NOT NULL") == "t"

    # Recorded now, so the next run skips it.
    again = _run(owner_db, tmp_path, sql.name)
    assert again.returncode == 0 and "SKIP 99_broken.sql" in again.stdout
