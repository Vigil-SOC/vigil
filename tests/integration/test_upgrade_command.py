"""``python -m core.upgrade`` against scratch databases on one local Postgres.

The empty-database run must end where a fresh init-plus-``create_all``
install does, by ``core.upgrade.schema``'s comparison. To rewrite the
committed head schema after adding a revision:

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
HEAD = "0001_baseline"


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


@pytest.fixture(scope="module")
def provisioned():
    with _scratch(True) as engine, engine.connect() as conn:
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
        assert not conn.execute(
            text(
                "SELECT has_table_privilege('vigil_app', 'public.alembic_version', "
                "'SELECT, INSERT, UPDATE, DELETE')"
            )
        ).scalar(), "the app role can move the schema revision"

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
        (None, {"restore_point": "rp-1"}, command.APPLY, [HEAD]),
        (None, {}, command.NEEDS_RESTORE_POINT, [HEAD]),
        (
            None,
            {"ack_version": "0.0.1", "ack_reason": "own backup"},
            command.NEEDS_RESTORE_POINT,
            [HEAD],
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
