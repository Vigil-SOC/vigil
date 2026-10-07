"""Rows the pre-#1276 isolation stub recorded as executed — issue #1686.

The stub reported success without isolating anything. Its rows still say
``executed`` and, through the ``isolate_host:<ip>`` idempotency key, make every
later isolation of that host look already done. The migration marks them
``failed``, which takes them out of the partial unique index and frees the key.

The scratch database holds only ``approval_actions`` (built from the model, so
the real columns and the real partial index) and a stub ``workflow_runs`` for
its foreign key.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from core.storage.models.workflow import ApprovalAction

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCRATCH_DB = "vigil_test_stub_isolations"

MOCK_MESSAGE = "Host has been network isolated successfully (MOCK)"
EXECUTED_AT = "2026-01-01 00:00:00"


def _url(database: str) -> str:
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


ADMIN_URL = _url("postgres")
SCRATCH_URL = _url(SCRATCH_DB)


def _postgres_available() -> bool:
    try:
        eng = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
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


def _load_migrate_schema():
    path = REPO_ROOT / "scripts" / "migrate_schema.py"
    spec = importlib.util.spec_from_file_location("vigil_migrate_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# (action_id, action_type, status, message, idempotency_key)
SEED_ROWS = [
    ("stub", "isolate_host", "executed", MOCK_MESSAGE, "isolate_host:10.0.0.5"),
    ("real", "isolate_host", "executed", "Isolated via EDR", "isolate_host:10.0.0.6"),
    ("other", "block_ip", "executed", "Blocked (MOCK)", "block_ip:10.0.0.7"),
    ("failed", "isolate_host", "failed", MOCK_MESSAGE, None),
]


@pytest.fixture
def approvals_db():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(SCRATCH_URL)
    with scratch.connect() as c:
        c.execute(text("CREATE TABLE workflow_runs (run_id VARCHAR(80) PRIMARY KEY)"))
        c.commit()
    ApprovalAction.__table__.create(scratch)

    with scratch.connect() as c:
        for action_id, action_type, status, message, key in SEED_ROWS:
            c.execute(
                text("""
                    INSERT INTO approval_actions
                        (action_id, action_type, title, description, target, reason,
                         created_by, status, executed_at, execution_result,
                         idempotency_key)
                    VALUES
                        (:id, :type, 't', 'd', :target, 'r', 'test', :status,
                         :executed_at, CAST(:result AS jsonb), :key)
                """),
                {
                    "id": action_id,
                    "type": action_type,
                    "target": (key or "isolate_host:10.0.0.8").split(":")[1],
                    "status": status,
                    "executed_at": EXECUTED_AT,
                    "result": '{"success": true, "message": "%s"}' % message,
                    "key": key,
                },
            )
        c.commit()

    yield scratch

    scratch.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _snapshot(conn):
    return conn.execute(
        text(
            "SELECT action_id, status, execution_result, executed_at "
            "FROM approval_actions ORDER BY action_id"
        )
    ).all()


def _run_step(engine):
    migrate_schema = _load_migrate_schema()
    with engine.connect() as conn:
        migrate_schema.fail_stub_isolation_actions(conn)
        conn.commit()
    return migrate_schema


def test_stub_row_is_failed_with_the_reason_and_the_original_message(approvals_db):
    migrate_schema = _run_step(approvals_db)

    with approvals_db.connect() as conn:
        status, result, executed_at = conn.execute(
            text(
                "SELECT status, execution_result, executed_at::text "
                "FROM approval_actions WHERE action_id = 'stub'"
            )
        ).one()

    assert status == "failed"
    assert result["message"] == MOCK_MESSAGE
    assert result["error"] == migrate_schema.STUB_ISOLATION_ERROR
    assert executed_at == EXECUTED_AT


def test_other_rows_are_untouched(approvals_db):
    with approvals_db.connect() as conn:
        before = {r.action_id: r for r in _snapshot(conn)}

    _run_step(approvals_db)

    with approvals_db.connect() as conn:
        after = {r.action_id: r for r in _snapshot(conn)}

    for action_id in ("real", "other", "failed"):
        assert after[action_id] == before[action_id]


def test_second_run_changes_nothing(approvals_db):
    _run_step(approvals_db)
    with approvals_db.connect() as conn:
        first = _snapshot(conn)

    _run_step(approvals_db)
    with approvals_db.connect() as conn:
        second = _snapshot(conn)

    assert second == first


def test_isolation_key_is_free_again_only_for_the_failed_stub_row(approvals_db):
    insert = text(
        "INSERT INTO approval_actions (action_id, action_type, title, description, "
        "target, reason, created_by, status, idempotency_key) "
        "VALUES (:id, 'isolate_host', 't', 'd', :target, 'r', 'test', 'pending', :key)"
    )

    with approvals_db.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                insert, {"id": "new1", "target": "10.0.0.5", "key": "isolate_host:10.0.0.5"}
            )
        conn.rollback()

    _run_step(approvals_db)

    with approvals_db.connect() as conn:
        conn.execute(
            insert, {"id": "new1", "target": "10.0.0.5", "key": "isolate_host:10.0.0.5"}
        )
        # A non-stub executed row keeps its key.
        with pytest.raises(IntegrityError):
            conn.execute(
                insert, {"id": "new2", "target": "10.0.0.6", "key": "isolate_host:10.0.0.6"}
            )
