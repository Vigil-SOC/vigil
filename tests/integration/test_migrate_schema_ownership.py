"""``scripts/migrate_schema.py`` on a schema with two owners, as Helm leaves it (#1245).

The chart's db-init Job applies the init SQL as the chart's database user, and
the backend's ``create_all`` then builds the remaining tables as ``vigil_app``.
ALTER and CREATE INDEX need a table's owner, so whichever role runs the script
may lack the privilege for some steps. Each step commits on its own: a step the
role cannot run is reported with who can run it, and the other steps still
commit. They used to share one transaction, so the first privilege error
discarded everything before it and failed everything after it.

The scratch database is built the same way. ``workflow_runs`` and
``episodic_distil_markers`` come from their init SQL, applied as the owner, and
``30_vigil_app_role.sql`` grants ``vigil_app`` what it grants on Helm.
``findings`` is created by ``vigil_app``, with the frozen default the models'
``create_all`` used to give it. The owner is the admin connection, a superuser
like the chart's user on the chart's own Postgres. One test uses a
non-superuser owner instead, as an external Postgres can have.
"""

import importlib.util
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "infra" / "database" / "init"
SCRATCH_DB = "vigil_test_migrate_owners"

FINDINGS_DDL = """
CREATE TABLE findings (
    finding_id VARCHAR(255) PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT '2026-01-01 00:00:00',
    updated_at TIMESTAMP NOT NULL DEFAULT '2026-01-01 00:00:00'
)
"""


def _parts():
    return {
        "user": os.getenv("POSTGRES_USER", "deeptempo"),
        "password": os.getenv(
            "POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"
        ),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
    }


def _url(database, user=None, password=None):
    parts = _parts()
    who = user if user is not None else parts["user"]
    pw = password if password is not None else parts["password"]
    return (
        f"postgresql://{quote(who, safe='')}:{quote(pw, safe='')}"
        f"@{parts['host']}:{parts['port']}/{database}"
    )


def _superuser_available():
    try:
        eng = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            return bool(
                c.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                ).scalar()
            )
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _superuser_available(),
        reason="requires a local PostgreSQL superuser, to create the roles",
    )
)


def _load_migrate_schema():
    """Import scripts/migrate_schema.py by path; ``scripts/`` is not a package."""
    path = REPO_ROOT / "scripts" / "migrate_schema.py"
    spec = importlib.util.spec_from_file_location("vigil_migrate_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_findings_default(column):
    """A stand-in step on a table vigil_app owns, so the runner's tests don't
    depend on which such steps the script has."""

    def step(conn):
        conn.execute(
            text(f"ALTER TABLE findings ALTER COLUMN {column} SET DEFAULT now()")
        )

    return (f"Set findings.{column} server default to now()", step)


def _set_missing_table_default(conn):
    conn.execute(text("ALTER TABLE cases ALTER COLUMN created_at SET DEFAULT now()"))


def _run_only(migrate, monkeypatch, *steps):
    """Limit the run to these steps: a name picks the script's own step."""
    by_name = {fn.__name__: (desc, fn) for desc, fn in migrate.MIGRATIONS}
    monkeypatch.setattr(
        migrate,
        "MIGRATIONS",
        [by_name[s] if isinstance(s, str) else s for s in steps],
    )


def _four_steps(migrate, monkeypatch):
    """One step on a table vigil_app owns; the script's two steps on tables the
    owner's SQL creates, one with the index missing and one already built in
    full; and a second step on vigil_app's table."""
    _run_only(
        migrate,
        monkeypatch,
        _set_findings_default("created_at"),
        "create_workflow_runs_triggered_by_index",
        "widen_episodic_distil_markers",
        _set_findings_default("updated_at"),
    )


def _create_table(name, table):
    """One CREATE TABLE out of an init SQL file.

    12_workflow_runs.sql also grants to a role named deeptempo, which a test
    server need not have.
    """
    sql = (INIT / name).read_text()
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.S)
    assert match, f"no CREATE TABLE {table} in {name}"
    return match.group(0)


def _provision(owner=None, owner_password=None):
    """Build the scratch database; ``owner`` None means the admin owns the SQL tables."""
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
        if owner:
            c.execute(
                text(
                    f"CREATE ROLE {owner} NOSUPERUSER LOGIN PASSWORD '{owner_password}'"
                )
            )
    admin.dispose()

    password = _parts()["password"].replace("'", "''")
    scratch = create_engine(_url(SCRATCH_DB))
    with scratch.begin() as conn:
        if owner:
            conn.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA public TO {owner}")
            conn.exec_driver_sql(f"SET ROLE {owner}")
        conn.exec_driver_sql(_create_table("12_workflow_runs.sql", "workflow_runs"))
        conn.exec_driver_sql((INIT / "26_episodic_memory.sql").read_text())
        conn.exec_driver_sql("RESET ROLE")
        conn.exec_driver_sql((INIT / "19_agent_ledger.sql").read_text())
        conn.exec_driver_sql((INIT / "30_vigil_app_role.sql").read_text())
        conn.exec_driver_sql(f"ALTER ROLE vigil_app PASSWORD '{password}'")
        conn.exec_driver_sql("SET ROLE vigil_app")
        conn.exec_driver_sql(FINDINGS_DDL)
        conn.exec_driver_sql("RESET ROLE")
    scratch.dispose()


def _drop(owner=None):
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        if owner:
            c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
    admin.dispose()


@pytest.fixture
def helm_db():
    """The chart's user is a superuser, as on the chart's own Postgres."""
    try:
        _provision()
        yield {
            "owner": _parts()["user"],
            "owner_url": _url(SCRATCH_DB),
            "app_url": _url(SCRATCH_DB, user="vigil_app"),
        }
    finally:
        _drop()


@pytest.fixture
def two_owner_db():
    """The SQL tables belong to a role that is not a superuser."""
    owner = f"vigil_test_owner_{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    try:
        _provision(owner, password)
        yield {
            "owner": owner,
            "owner_url": _url(SCRATCH_DB, user=owner, password=password),
            "app_url": _url(SCRATCH_DB, user="vigil_app"),
        }
    finally:
        _drop(owner)


def _state():
    engine = create_engine(_url(SCRATCH_DB))
    try:
        with engine.connect() as c:
            defaults = dict(
                c.execute(
                    text(
                        "SELECT column_name, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'findings' "
                        "AND column_name IN ('created_at', 'updated_at')"
                    )
                ).all()
            )
            index = c.execute(
                text("SELECT to_regclass('idx_workflow_runs_triggered_by')")
            ).scalar()
    finally:
        engine.dispose()
    return defaults, index


def _numbers(steps):
    return [step[0] for step in steps]


def test_a_step_the_role_cannot_run_is_skipped_and_the_rest_commit(
    helm_db, monkeypatch, caplog
):
    migrate = _load_migrate_schema()
    _four_steps(migrate, monkeypatch)

    with caplog.at_level(logging.INFO):
        result = migrate.run_migrations(helm_db["app_url"])

    assert _numbers(result["applied"]) == [1, 3, 4]
    assert result["failed"] == []
    [(number, _, role)] = result["skipped"]
    assert number == 2
    assert role == f"{helm_db['owner']} (owner of workflow_runs) or a superuser"
    # What vigil_app could do is in the database, not rolled back with the skip.
    defaults, index = _state()
    assert defaults == {"created_at": "now()", "updated_at": "now()"}
    assert index is None
    # Each step is numbered once, in order.
    assert re.findall(r"\[(\d)/4\]", caplog.text) == ["1", "2", "3", "4"]


def test_once_the_owner_has_run_it_vigil_app_runs_clean(helm_db, monkeypatch):
    migrate = _load_migrate_schema()
    _four_steps(migrate, monkeypatch)

    as_owner = migrate.run_migrations(helm_db["owner_url"])
    assert as_owner["skipped"] == [] and as_owner["failed"] == []

    again = migrate.run_migrations(helm_db["app_url"])
    assert again["skipped"] == [] and again["failed"] == []
    assert _numbers(again["applied"]) == [1, 2, 3, 4]
    assert _state()[1] == "idx_workflow_runs_triggered_by"


def test_a_failed_step_rolls_back_alone(helm_db, monkeypatch):
    migrate = _load_migrate_schema()
    # No cases table here: the middle step fails for a reason other than privilege.
    _run_only(
        migrate,
        monkeypatch,
        _set_findings_default("created_at"),
        ("Set cases.created_at server default to now()", _set_missing_table_default),
        _set_findings_default("updated_at"),
    )

    result = migrate.run_migrations(helm_db["app_url"])

    assert _numbers(result["applied"]) == [1, 3]
    assert _numbers(result["failed"]) == [2]
    assert result["skipped"] == []
    assert _state()[0] == {"created_at": "now()", "updated_at": "now()"}


def test_with_a_non_superuser_owner_each_role_names_the_other(
    two_owner_db, monkeypatch
):
    migrate = _load_migrate_schema()
    _four_steps(migrate, monkeypatch)

    as_owner = migrate.run_migrations(two_owner_db["owner_url"])
    assert _numbers(as_owner["applied"]) == [2, 3]
    assert [(n, role) for n, _, role in as_owner["skipped"]] == [
        (1, "vigil_app (owner of findings) or a superuser"),
        (4, "vigil_app (owner of findings) or a superuser"),
    ]

    as_app = migrate.run_migrations(two_owner_db["app_url"])
    assert as_app["skipped"] == [] and as_app["failed"] == []
    defaults, index = _state()
    assert defaults == {"created_at": "now()", "updated_at": "now()"}
    assert index == "idx_workflow_runs_triggered_by"
