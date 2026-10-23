"""``python -m core.upgrade`` against scratch databases on one local Postgres.

The empty-database run must end where a fresh init-plus-``create_all``
install does once the revisions after the baseline have run on it, and at
the committed head schema, by ``core.upgrade.schema``'s comparison. Models
include tables those revisions also build, so ``create_all`` is no longer
``0001_baseline``; ``baseline_schema.json`` stays that frozen revision. To
rewrite the committed head schema after adding a revision:

    VIGIL_WRITE_HEAD_SCHEMA=1 pytest tests/integration/test_upgrade_command.py
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytestmark = [pytest.mark.integration, pytest.mark.database]

from core.upgrade import command  # noqa: E402
from core.upgrade.schema import compare, schema_state  # noqa: E402
from scripts.generate_schema_snapshot import (  # noqa: E402
    ScratchDatabaseUnavailable,
    postgres_skip_reason,
    scratch_database,
)

SEEDED = (
    "roles",
    "sla_policies",
    "case_templates",
    "system_config",
    "user_preferences",
)
# Every revision, oldest first: what an empty database has pending.
REVISIONS = [
    s.revision for s in reversed(list(command.script_directory().walk_revisions()))
]
HEAD = REVISIONS[-1]


@contextmanager
def _scratch(provision: bool):
    reason = postgres_skip_reason()
    if reason:
        pytest.skip(reason)
    try:
        with scratch_database(provision=provision) as (_, engine):
            yield engine
    except ScratchDatabaseUnavailable as e:
        pytest.skip(str(e))


def _app_can_move_revision(conn) -> bool:
    # The app may read the revision (core/audit/trail.py), never write it.
    return conn.execute(
        text(
            "SELECT has_table_privilege('vigil_app', 'public.alembic_version', "
            "'INSERT, UPDATE, DELETE, TRUNCATE')"
        )
    ).scalar()


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _rows(conn) -> dict:
    # created_at/updated_at are install time; the schema_version stamp is the
    # init path's, which revisions retire.
    return {
        table: sorted(
            json.dumps(row, sort_keys=True)
            for (row,) in conn.execute(
                text(
                    f"SELECT to_jsonb(t) - 'created_at' - 'updated_at' FROM {table} t "
                    "WHERE to_jsonb(t) ->> 'key' IS DISTINCT FROM 'schema_version'"
                )
            )
        )
        for table in SEEDED
    }


def _run(engine, tmp_path, **kw) -> tuple[int, dict]:
    out = io.StringIO()
    code = command.run(_url(engine), upgrade_dir=tmp_path / "upgrade", out=out, **kw)
    return code, json.loads(out.getvalue())


def _history(tmp_path) -> list[dict]:
    path = tmp_path / "upgrade" / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _version(engine):
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.alembic_version')")
        ).scalar()
        if exists is None:
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _apply_later_revisions(engine) -> None:
    """Each revision after the baseline, as SQL, on a create_all-built install:
    what that install's first upgrade runs once it is adopted.

    ``0002_audit_trail`` grants on ``alembic_version``. ``create_all`` does not
    build that table; the upgrade command does, before the revisions run.
    """
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS public.alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
            for path in sorted((command.PACKAGE_DIR / "versions").glob("*.sql")):
                if path.stem != "0001_baseline":
                    cursor.execute(path.read_text())
        raw.commit()
    finally:
        raw.close()


@pytest.fixture(scope="module")
def provisioned():
    with _scratch(True) as engine:
        _apply_later_revisions(engine)
        with engine.connect() as conn:
            yield schema_state(conn), _rows(conn)


@pytest.fixture
def empty():
    with _scratch(False) as engine:
        yield engine


def test_empty_database_matches_fresh_install(provisioned, empty, tmp_path):
    expected_state, expected_rows = provisioned
    code, report = _run(empty, tmp_path, restore_point="rp-1")
    assert code == command.EXIT_OK, report
    assert report["outcome"] == "applied" and report["restore_point"] == "rp-1"
    assert [h["outcome"] for h in _history(tmp_path)] == ["applied"]

    with empty.connect() as conn:
        state = schema_state(conn)
        assert compare(expected_state, state) == []
        assert _rows(conn) == expected_rows
        assert conn.execute(
            text(
                "SELECT has_table_privilege('vigil_app', 'public.alembic_version', "
                "'SELECT')"
            )
        ).scalar(), "the app role reads the revision at startup"
        assert not _app_can_move_revision(conn), "the app role can move the revision"

    if os.environ.get("VIGIL_WRITE_HEAD_SCHEMA"):
        command.HEAD_SCHEMA.write_text(
            json.dumps(state, indent=1, sort_keys=True) + "\n"
        )
    assert json.loads(command.HEAD_SCHEMA.read_text()) == state, (
        "core/upgrade/head_schema.json is stale. Rewrite it with "
        "VIGIL_WRITE_HEAD_SCHEMA=1 pytest tests/integration/test_upgrade_command.py"
    )

    # A second run is a restart: exit 0 and no history line, even when handed a
    # restore point it does not need.
    for kw in ({}, {"restore_point": "rp-2"}):
        code, report = _run(empty, tmp_path, **kw)
        assert (code, report["outcome"]) == (command.EXIT_OK, "restart")
    assert len(_history(tmp_path)) == 1


def test_lock_held_elsewhere_refuses(empty, tmp_path, monkeypatch):
    monkeypatch.setattr(command, "LOCK_WAIT_SECONDS", 1.0)
    with empty.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": command.LOCK_KEY})
        code, report = _run(empty, tmp_path, restore_point="rp-1")
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": command.LOCK_KEY})
    assert code == command.EXIT_BUSY
    assert report["reason"] == "another upgrade is running"
    assert _version(empty) is None


def _at_head(engine, tmp_path):
    assert _run(engine, tmp_path / "setup", restore_point="rp-0")[0] == command.EXIT_OK


def _unstamped(engine, _tmp_path):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE public.findings (id int)"))


def _newer(engine, tmp_path):
    _at_head(engine, tmp_path)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))


@pytest.mark.parametrize(
    "setup, kw, verdict, pending",
    [
        (None, {"restore_point": "rp-1"}, command.APPLY, REVISIONS),
        (None, {}, command.NEEDS_RESTORE_POINT, REVISIONS),
        (
            None,
            {"ack_version": "0.0.1", "ack_reason": "own backup"},
            command.NEEDS_RESTORE_POINT,
            REVISIONS,
        ),
        (_at_head, {}, command.RESTART, []),
        (_unstamped, {"restore_point": "rp-1"}, command.REFUSE, []),
        (_newer, {"restore_point": "rp-1"}, command.REFUSE, []),
    ],
    ids=["empty", "empty-no-point", "empty-stale-ack", "at-head", "unstamped", "newer"],
)
def test_check_matches_the_apply_that_follows(
    empty, tmp_path, setup, kw, verdict, pending
):
    if setup:
        setup(empty, tmp_path)
    before = _version(empty)

    code, report = _run(empty, tmp_path, check=True, **kw)
    assert (report["verdict"], report["pending"]) == (verdict, pending), report
    assert report["drift"] == []
    assert "free_bytes" not in report
    assert not (tmp_path / "upgrade").exists(), "--check wrote the upgrade directory"
    assert _version(empty) == before

    code, outcome = _run(empty, tmp_path, **kw)
    if verdict == command.APPLY:
        assert (code, outcome["outcome"], _version(empty)) == (0, "applied", HEAD)
    elif verdict == command.RESTART:
        assert (code, outcome["outcome"]) == (0, "restart")
        assert _history(tmp_path) == []
    else:
        assert (code, outcome["outcome"]) == (command.EXIT_REFUSED, "refused")
        assert outcome["reason"] == report["reason"]
        assert _version(empty) == before


def test_acknowledgement_for_this_version_applies(empty, tmp_path):
    code, report = _run(
        empty, tmp_path, ack_version=command.__version__, ack_reason="own backup"
    )
    assert code == command.EXIT_OK
    assert (report["restore_point"], report["reason"]) == ("overridden", "own backup")
