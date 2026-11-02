"""vigil_app cannot create objects in public; python -m core.upgrade owns the schema.

Needs a real Postgres: set ``VIGIL_TEST_POSTGRES_DSN`` to the owner's
``postgresql+psycopg2://`` login on a throwaway server. Each test makes its own
database. ``vigil_app`` is cluster-wide, so it is given a new password.
"""

import io
import json
import os
import secrets

import pytest

DSN = os.environ.get("VIGIL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_POSTGRES_DSN not set")

PREVIOUS_HEAD = "0019_finding_ip_addresses"
APP_PASSWORD = secrets.token_hex(16)


@pytest.fixture
def owner_url():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"vigil_no_create_{secrets.token_hex(4)}"
    admin = create_engine(DSN, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    try:
        yield make_url(DSN).set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _engine(url):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    return create_engine(url, poolclass=NullPool)


def _upgrade(url, tmp_path, check=False):
    from core.upgrade import command
    from core.version import __version__

    out = io.StringIO()
    code = command.run(
        url.render_as_string(hide_password=False),
        check=check,
        upgrade_dir=tmp_path,
        ack_version=__version__,
        ack_reason="test",
        out=out,
    )
    return code, json.loads(out.getvalue())


def _migrate_to(url, target):
    from alembic.runtime.environment import EnvironmentContext

    from core.upgrade import command

    script = command.script_directory()

    def upgrade(rev, _context):
        return script._upgrade_revs(target, rev)

    engine = _engine(url)
    try:
        with engine.connect() as conn, EnvironmentContext(
            command.alembic_config(), script, fn=upgrade, destination_rev=target
        ) as env:
            env.configure(
                connection=conn,
                transaction_per_migration=True,
                version_table_schema="public",
            )
            with env.begin_transaction():
                env.run_migrations()
    finally:
        engine.dispose()


def _owner_runs(url, statement):
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


def _app_runs(url, statement):
    from sqlalchemy import text

    _owner_runs(url, f"ALTER ROLE vigil_app PASSWORD '{APP_PASSWORD}'")
    app = _engine(url.set(username="vigil_app", password=APP_PASSWORD))
    try:
        with app.begin() as conn:
            conn.execute(text(statement))
    finally:
        app.dispose()


def _assert_app_cannot_create(url):
    from psycopg2.errors import InsufficientPrivilege
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as refused:
        _app_runs(url, "CREATE TABLE public.not_from_upgrade (id int)")
    assert isinstance(refused.value.orig, InsufficientPrivilege)


def _assert_at_head_without_drift(url, tmp_path):
    code, report = _upgrade(url, tmp_path, check=True)
    assert (code, report["verdict"], report["drift"]) == (0, "restart", [])


def test_a_fresh_install_refuses_the_app_role(owner_url, tmp_path):
    code, report = _upgrade(owner_url, tmp_path)
    assert (code, report["outcome"]) == (0, "applied")

    _assert_app_cannot_create(owner_url)
    _assert_at_head_without_drift(owner_url, tmp_path)


def test_upgrading_from_the_previous_head_takes_it_away(owner_url, tmp_path):
    _migrate_to(owner_url, PREVIOUS_HEAD)
    _app_runs(owner_url, "CREATE TABLE public.before_upgrade (id int)")
    _app_runs(owner_url, "DROP TABLE public.before_upgrade")

    code, report = _upgrade(owner_url, tmp_path)
    assert (code, report["outcome"]) == (0, "applied")

    _assert_app_cannot_create(owner_url)
    _assert_at_head_without_drift(owner_url, tmp_path)


def test_a_role_that_already_lacks_it_upgrades_and_restarts(owner_url, tmp_path):
    _migrate_to(owner_url, PREVIOUS_HEAD)
    _owner_runs(owner_url, "REVOKE CREATE ON SCHEMA public FROM vigil_app")

    code, report = _upgrade(owner_url, tmp_path)
    assert (code, report["outcome"]) == (0, "applied")
    code, report = _upgrade(owner_url, tmp_path)
    assert (code, report["outcome"]) == (0, "restart")

    _assert_app_cannot_create(owner_url)
    _assert_at_head_without_drift(owner_url, tmp_path)
