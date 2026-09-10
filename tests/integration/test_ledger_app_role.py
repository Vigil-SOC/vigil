"""vigil_app may INSERT and SELECT agent_events; UPDATE and DELETE fail at Postgres.

Issue #824. Connects as the app role, not the owner, and does not consult
has_table_privilege.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

from core.workflows import run_cancel

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "infra" / "database" / "init"
SCRATCH_DB = "vigil_test_ledger_grants"
INSUFFICIENT_PRIVILEGE = "42501"


def _parts():
    return {
        "user": os.getenv("POSTGRES_USER", "deeptempo"),
        "password": os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
    }


def _url(database: str, user: str | None = None, password: str | None = None) -> str:
    parts = _parts()
    who = user if user is not None else parts["user"]
    pw = password if password is not None else parts["password"]
    return (
        f"postgresql://{quote(who, safe='')}:{quote(pw, safe='')}"
        f"@{parts['host']}:{parts['port']}/{database}"
    )


def _postgres_available() -> bool:
    try:
        eng = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
        with eng.connect():
            return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _postgres_available(),
        reason="requires a local PostgreSQL (docker compose up -d postgres)",
    )
)


def _apply_sql(conn, name: str) -> None:
    conn.exec_driver_sql((INIT / name).read_text())


def _pgcode(exc: ProgrammingError) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None)


@pytest.fixture(scope="module")
def app_engine():
    """Owner provisions the role; tests connect as vigil_app."""
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    owner = create_engine(_url(SCRATCH_DB))
    password = _parts()["password"]
    with owner.connect() as conn:
        _apply_sql(conn, "19_agent_ledger.sql")
        _apply_sql(conn, "30_vigil_app_role.sql")
        escaped = password.replace("'", "''")
        conn.exec_driver_sql(f"ALTER ROLE vigil_app PASSWORD '{escaped}'")
        conn.commit()

    app = create_engine(_url(SCRATCH_DB, user="vigil_app", password=password))
    yield app

    app.dispose()
    owner.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _insert_run(conn, run_id: uuid.UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO agent_events "
            "(run_id, seq, run_kind, kind, payload, schema_version) "
            "VALUES (:run_id, 0, 'hunt', 'run', CAST(:payload AS jsonb), 1)"
        ),
        {"run_id": str(run_id), "payload": json.dumps({"seed": str(run_id)})},
    )


def test_vigil_app_insert_succeeds_update_and_delete_fail(app_engine):
    run_id = uuid.uuid4()
    with app_engine.connect() as conn:
        _insert_run(conn, run_id)
        conn.commit()
        kind = conn.execute(
            text("SELECT kind FROM agent_events WHERE run_id = :r"),
            {"r": str(run_id)},
        ).scalar_one()
    assert kind == "run"

    with pytest.raises(ProgrammingError) as updated:
        with app_engine.connect() as conn:
            conn.execute(
                text("UPDATE agent_events SET kind = 'tampered' WHERE run_id = :r"),
                {"r": str(run_id)},
            )
            conn.commit()
    assert _pgcode(updated.value) == INSUFFICIENT_PRIVILEGE

    with pytest.raises(ProgrammingError) as deleted:
        with app_engine.connect() as conn:
            conn.execute(
                text("DELETE FROM agent_events WHERE run_id = :r"),
                {"r": str(run_id)},
            )
            conn.commit()
    assert _pgcode(deleted.value) == INSUFFICIENT_PRIVILEGE


def test_force_terminal_appends_as_vigil_app(app_engine, monkeypatch):
    run_id = uuid.uuid4()
    with app_engine.connect() as conn:
        _insert_run(conn, run_id)
        conn.commit()

    monkeypatch.setattr(run_cancel, "get_db_session", sessionmaker(bind=app_engine))

    assert run_cancel.force_terminal(str(run_id), "stopped from the console") is True

    with app_engine.connect() as conn:
        kinds = (
            conn.execute(
                text("SELECT kind FROM agent_events WHERE run_id = :r ORDER BY seq"),
                {"r": str(run_id)},
            )
            .scalars()
            .all()
        )
    assert kinds == ["run", "terminal"]
