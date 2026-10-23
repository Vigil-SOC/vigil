"""The audit trail (core/audit/trail.py) on a database ``python -m core.upgrade`` built.

Writes run as ``vigil_app`` (``SET ROLE``), so the grants are the ones served.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.audit import trail
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == 0, report
        yield engine


@pytest.fixture
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
    return engine


@contextmanager
def _app(engine):
    """A connection acting as vigil_app; the caller commits."""
    with engine.connect() as conn:
        conn.execute(text("SET ROLE vigil_app"))
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute(text("RESET ROLE"))
            conn.commit()


def _seqs(engine) -> list[int]:
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT seq FROM audit_trail ORDER BY seq")).scalars()
        )


def _verify(engine) -> trail.Verification:
    with _app(engine) as conn:
        return trail.verify(conn)


def test_concurrent_appends_chain_without_gap_or_fork(db):
    held = threading.Event()
    errors: list[BaseException] = []

    def second():
        try:
            with _app(db) as conn:
                held.wait(5)
                trail.append(conn, {"action": "second"})  # waits on the first's lock
                conn.commit()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    with _app(db) as rolled_back, _app(db) as first:
        # A rolled-back append leaves no gap: its seq is handed out again.
        trail.append(rolled_back, {"action": "abandoned"})
        rolled_back.rollback()
        trail.append(first, {"action": "first"})
        worker = threading.Thread(target=second)
        worker.start()
        held.set()
        worker.join(0.5)
        assert worker.is_alive(), "the second append did not wait for the lock"
        first.commit()
        worker.join(10)
    assert not errors, errors
    assert _seqs(db) == [1, 2]
    assert _verify(db).ok


def test_hashed_tamper_fails_verify_and_beside_edit_does_not(db):
    with _app(db) as conn:
        for i in range(3):
            trail.append(conn, {"action": "x", "i": i}, source_ip="10.0.0.1")
        conn.commit()
        conn.execute(text("UPDATE audit_trail SET source_ip = NULL, personal = '{}'"))
        conn.commit()
    assert _verify(db).ok

    with db.begin() as owner:  # only the owner can reach a hashed column
        owner.execute(
            text(
                """UPDATE audit_trail SET payload = '{"action": "y", "i": 1}' WHERE seq = 2"""
            )
        )
    result = _verify(db)
    assert (result.first_bad, result.reason) == (
        2,
        "entry_hash does not match its content",
    )

    with db.begin() as owner:
        owner.execute(text("DELETE FROM audit_trail WHERE seq = 2"))
    assert _verify(db).first_bad == 2


def test_app_cannot_update_hashed_columns_or_delete(db):
    with _app(db) as conn:
        trail.append(conn, {"action": "x"})
        trail.append(conn, {"action": "x"})
        conn.commit()
    for sql in (
        "UPDATE audit_trail SET payload = '{}'",
        "UPDATE audit_trail SET occurred_at = now()",
        "UPDATE audit_trail SET entry_hash = ''",
        "DELETE FROM audit_trail",
        "TRUNCATE audit_trail",
        "UPDATE audit_trail_anchor SET seq = 5",
    ):
        with _app(db) as conn, pytest.raises(DBAPIError) as caught:
            conn.execute(text(sql))
        assert caught.value.orig.pgcode == INSUFFICIENT_PRIVILEGE, sql

    # The one delete: a whole prefix, and verify starts at the moved anchor.
    with _app(db) as conn:
        assert conn.execute(text("SELECT audit_trail_delete_prefix(1)")).scalar() == 1
        conn.commit()
        assert trail.append(conn, {"action": "after"}).seq == 3
        conn.commit()
    assert _seqs(db) == [2, 3]
    result = _verify(db)
    assert result.ok and (result.first, result.last) == (2, 3)


def test_a_temp_table_cannot_stand_in_for_the_trail(db):
    with _app(db) as conn:
        trail.append(conn, {"action": "real"})
        conn.commit()
        conn.execute(
            text("CREATE TEMP TABLE audit_trail (seq bigint, entry_hash text)")
        )
        conn.execute(text("INSERT INTO pg_temp.audit_trail VALUES (500, 'forged')"))
        with pytest.raises(DBAPIError, match="no entry 500"):
            conn.execute(text("SELECT audit_trail_delete_prefix(500)"))
        conn.rollback()
        conn.execute(
            text("CREATE TEMP TABLE audit_trail (seq bigint, entry_hash text)")
        )
        conn.execute(text("INSERT INTO pg_temp.audit_trail VALUES (500, 'forged')"))
        conn.execute(
            text("""INSERT INTO public.audit_trail (payload) VALUES ('{"a": 1}')""")
        )
        conn.commit()
        conn.execute(text("DROP TABLE pg_temp.audit_trail"))  # pooled
        conn.commit()
    assert _seqs(db) == [1, 2]
    assert _verify(db).ok


def test_a_held_row_the_trail_refuses_does_not_block_the_rest(db):
    with db.begin() as owner:  # bypasses hold()'s check, as a bad row would
        owner.execute(
            text(
                "INSERT INTO audit_trail_held (occurred_at, payload) "
                """VALUES (now(), '{"seq": 7}'), (now(), '{"action": "ok"}')"""
            )
        )
    with _app(db) as conn:
        assert trail.drain_late(conn) == 1
        conn.commit()
        left = (
            conn.execute(text("SELECT payload FROM audit_trail_held")).scalars().all()
        )
    assert left == [{"seq": 7}]
    assert _seqs(db) == [1]


def test_late_row_promotes_once_with_its_original_time(db):
    then = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    with _app(db) as conn:
        trail.append(conn, {"action": "before"})
        trail.hold(conn, {"action": "user.disable"}, then, source_ip="10.1.1.1")
        conn.commit()
        assert trail.drain_late(conn) == 1
        conn.commit()
        assert trail.drain_late(conn) == 0
        rows = conn.execute(
            text(
                "SELECT seq, occurred_at, late, payload, source_ip FROM audit_trail ORDER BY seq"
            )
        ).all()
        assert conn.execute(text("SELECT count(*) FROM audit_trail_held")).scalar() == 0
    assert [tuple(r) for r in rows[1:]] == [
        (2, then, True, {"action": "user.disable"}, "10.1.1.1")
    ]
    assert _verify(db).ok

    # The late flag is inside the hash.
    with db.begin() as owner:
        owner.execute(text("UPDATE audit_trail SET late = false WHERE seq = 2"))
    assert _verify(db).first_bad == 2


def test_failures_in_a_window_collapse_to_one_entry_until_it_closes(db):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = trail.failure_key("203.0.113.9", "admin")
    payload = {"action": "sign_in.failed"}
    with _app(db) as conn:
        first = trail.collapse(conn, key, payload, now=t0, attempted_name="admin")
        conn.commit()
        assert first is not None and first.seq == 1
        for s in (10, 59):
            when = t0 + timedelta(seconds=s)
            assert trail.collapse(conn, key, payload, now=when) is None
            conn.commit()
        assert _seqs(db) == [1]

        assert trail.close_windows(conn, now=t0 + timedelta(seconds=30)) == 0
        assert trail.close_windows(conn, now=t0 + timedelta(seconds=61)) == 1
        conn.commit()
        summary = conn.execute(
            text("SELECT payload, attempted_name FROM audit_trail WHERE seq = 2")
        ).one()
    assert summary.attempted_name == "admin"
    assert summary.payload["collapsed"]["count"] == 3
    assert summary.payload["collapsed"]["first_seq"] == 1
    assert _verify(db).ok


def test_a_new_failure_after_the_window_closes_it_and_starts_another(db):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = trail.refusal_key("run-1", "not permitted")
    with _app(db) as conn:
        trail.collapse(conn, key, {"action": "refused"}, now=t0)
        trail.collapse(conn, key, {"action": "refused"}, now=t0 + timedelta(seconds=5))
        later = trail.collapse(
            conn, key, {"action": "refused"}, now=t0 + timedelta(seconds=90)
        )
        conn.commit()
    # The first, the closed window's count of 2, then the new window's first.
    assert later.seq == 3 and _seqs(db) == [1, 2, 3]
    assert _verify(db).ok


def test_every_thousandth_append_writes_a_checkpoint(db):
    with _app(db) as conn:
        conn.execute(
            text(
                "INSERT INTO audit_trail (payload) "
                """SELECT '{"action": "bulk"}' FROM generate_series(1, 999)"""
            )
        )
        entry = trail.append(conn, {"action": "thousandth"})
        conn.commit()
        payload = conn.execute(
            text("SELECT payload FROM audit_trail WHERE seq = 1001")
        ).scalar_one()
    assert entry.seq == 1000
    assert payload["checkpoint"] == {"seq": 1000, "hash": entry.entry_hash}
    assert _verify(db).ok


def test_trail_ready_follows_the_revision(db):
    with _app(db) as conn:
        assert trail.trail_ready(conn)
    with db.begin() as owner:
        owner.execute(text("UPDATE alembic_version SET version_num = '0001_baseline'"))
    try:
        with _app(db) as conn:
            assert not trail.trail_ready(conn)
    finally:
        with db.begin() as owner:
            owner.execute(
                text("UPDATE alembic_version SET version_num = :v"),
                {"v": trail.REVISION},
            )
