"""The old audit stores copied onto the trail by ``python -m core.upgrade`` (#3309).

A scratch database is brought to the revision before the copy, seeded, then
upgraded twice through the command, as ``tests/integration/test_upgrade_command.py``.
The second run is a restart; the copy itself is run again directly to show it
adds nothing.
"""

from __future__ import annotations

import importlib.util
import json

import pytest
from alembic.runtime.environment import EnvironmentContext
from sqlalchemy import text

from core.audit import trail
from core.upgrade import command
from tests.integration.test_upgrade_command import _history, _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

_spec = importlib.util.spec_from_file_location(
    "legacy_audit_copy", command.PACKAGE_DIR / "versions/0018_legacy_audit_copy.py"
)
copy_revision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(copy_revision)
BEFORE = copy_revision.down_revision


def _migrate_to(engine, destination: str) -> None:
    script = command.script_directory()
    with engine.connect() as conn, EnvironmentContext(
        command.alembic_config(),
        script,
        fn=lambda rev, _ctx: script._upgrade_revs(destination, rev),
        destination_rev=destination,
    ) as env:
        env.configure(
            connection=conn,
            transaction_per_migration=True,
            version_table_schema="public",
        )
        with env.begin_transaction():
            env.run_migrations()


_SEED = """
INSERT INTO principals (principal_id, kind, label) VALUES
    ('p-alice', 'person', 'Alice'), ('p-bob', 'person', 'Bob');
INSERT INTO users (user_id, username, email, password_hash, full_name, role_id,
                   principal_id)
VALUES ('u-alice', 'alice', 'a@x.test', 'x', 'Alice', 'role-analyst', 'p-alice'),
       ('u-bob', 'bob', 'b@x.test', 'x', 'Bob', 'role-analyst', 'p-bob');
INSERT INTO case_audit_logs (audit_id, entity_type, entity_id, action, field_name,
                             old_value, new_value, change_summary, changed_by,
                             ip_address, "timestamp")
VALUES (1, 'case', 'CASE-1', 'update', 'status', 'open', 'closed', NULL, 'alice',
        '10.0.0.1', '2026-01-02 03:04:05'),
       (2, 'case', 'CASE-1', 'update', 'escalation', NULL, NULL,
        'SOC Analyst escalated this case to bob: noisy', 'SOC Analyst', NULL,
        '2026-01-02 03:04:06'),
       (3, 'finding', 'F-1', 'create', NULL, NULL, NULL, NULL, 'system', NULL,
        '2026-01-02 03:04:07');
INSERT INTO config_audit_log (id, config_type, config_key, action, old_value,
                              new_value, changed_by, "timestamp")
VALUES (1, 'integration', 'github', 'update',
        '{"token": "ghp_old", "org": "a"}', '{"token": "ghp_new", "org": "b"}',
        'u-bob', '2026-01-03 00:00:00'),
       (2, 'system', 'k', 'create', NULL, '{"api_key": "sk-1", "host": "h"}',
        'orchestrator', '2026-01-03 00:00:01'),
       (3, 'integration', 'since-removed', 'create', NULL,
        '{"api_key": "sk-gone", "host": "h"}', 'nobody', '2026-01-03 00:00:02');
INSERT INTO investigations (investigation_id, workflow_id, trigger_type,
    trigger_ids, status, workdir, current_step, total_steps, iteration_count,
    max_iterations, input_tokens, output_tokens, cost_usd, max_cost_usd, priority,
    max_runtime_seconds, error_count)
VALUES ('INV-1', 'wf', 'manual', '[]', 'done', '/tmp/x', 0, 0, 0, 1, 0, 0, 0, 1,
        'low', 60, 0);
INSERT INTO investigation_logs (id, investigation_id, event_type, details,
                                tokens_used, "timestamp")
VALUES (1, 'INV-1', 'tool_call', '{"tool": "x"}', 5, '2026-01-04 00:00:00');
"""


@pytest.fixture(scope="module")
def copied(tmp_path_factory):
    with _scratch(False) as engine:
        _migrate_to(engine, BEFORE)
        with engine.begin() as conn:
            for statement in _SEED.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
        tmp = tmp_path_factory.mktemp("up")
        first = _run(engine, tmp, restore_point="rp-1")
        second = _run(engine, tmp)
        yield engine, first, second, _history(tmp)


def _entries(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT payload, actor_text, operator_text, source_ip, occurred_at
            FROM audit_trail WHERE payload ? 'legacy' ORDER BY seq
        """)).mappings()
        return [dict(r) for r in rows]


def _by_key(engine) -> dict[tuple[str, int], dict]:
    return {
        (e["payload"]["legacy"]["table"], e["payload"]["legacy"]["id"]): e
        for e in _entries(engine)
    }


def test_each_row_is_copied_once_and_the_second_run_adds_nothing(copied):
    engine, (code, report), (code2, report2), history = copied
    assert code == command.EXIT_OK, report
    assert (code2, report2["outcome"]) == (command.EXIT_OK, "restart")
    assert len(_entries(engine)) == 7
    (body,) = [
        r["report"]
        for r in report["reports"]
        if r["revision"] == "0018_legacy_audit_copy"
    ]
    assert body["passed"] and body["copied"] == 7
    assert body["sources"]["case_audit_logs"] == {"rows": 3, "on_trail": 3, "copied": 3}

    # The copy run again on the same trail appends nothing.
    with engine.begin() as conn:
        again = copy_revision.copy(conn)
    assert again["copied"] == 0 and again["passed"]
    assert len(_entries(engine)) == 7
    assert len(history) == 1


def test_links_follow_decision_14(copied):
    engine = copied[0]
    found = _by_key(engine)
    by_name = found[("case_audit_logs", 1)]
    assert by_name["payload"]["actor"] == "p-alice"
    assert by_name["payload"]["attribution"] == trail.ATTRIBUTION_MATCHED_BY_NAME
    assert by_name["payload"]["case_id"] == "CASE-1"
    assert by_name["actor_text"] == "alice" and by_name["source_ip"] == "10.0.0.1"
    assert by_name["operator_text"] == "open → closed"
    assert by_name["occurred_at"].isoformat() == "2026-01-02T03:04:05+00:00"

    soc = found[("case_audit_logs", 2)]["payload"]
    assert "actor" not in soc
    assert soc["attribution"] == trail.ATTRIBUTION_UNVERIFIED_LEGACY
    assert "SOC Analyst" not in json.dumps(soc)

    system = found[("case_audit_logs", 3)]["payload"]
    assert (system["actor"], system["attribution"]) == ("system", "verified")
    assert "case_id" not in system

    internal = found[("config_audit_log", 1)]["payload"]
    assert (internal["actor"], internal["attribution"]) == ("p-bob", "verified")
    assert found[("config_audit_log", 2)]["payload"]["actor"] == "system"

    inv = found[("investigation_logs", 1)]
    assert "actor" not in inv["payload"] and inv["actor_text"] is None
    assert inv["payload"]["investigation_id"] == "INV-1"
    assert inv["payload"]["attribution"] == trail.ATTRIBUTION_UNVERIFIED_LEGACY


def test_config_secrets_are_masked(copied):
    found = _by_key(copied[0])
    integration = found[("config_audit_log", 1)]["payload"]
    assert integration["old_value"] == {"org": "a"}
    assert integration["new_value"] == {"org": "b"}
    system = found[("config_audit_log", 2)]["payload"]["new_value"]
    assert system["api_key"] != "sk-1" and system["host"] == "h"
    # An integration no longer registered: its redactor knows no fields.
    gone = found[("config_audit_log", 3)]["payload"]["new_value"]
    assert gone["api_key"] != "sk-gone" and gone["host"] == "h"


def test_the_chain_verifies_and_the_case_filter_finds_the_copy(copied):
    engine = copied[0]
    with engine.connect() as conn:
        assert trail.verify(conn).ok
        rows = trail.read(conn, trail.TrailQuery(object_ref="CASE-1"))
    assert [r["payload"]["legacy"]["id"] for r in rows] == [1, 2]
    assert rows[0]["sentence"] == "Changed status on the case"


def test_the_command_sets_the_stamp_after_the_revision(copied, tmp_path):
    engine, (_code, report), *_ = copied
    stamp = report["legacy_audit_read_only"]
    assert stamp["set"], stamp
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM audit_legacy_copy")).mappings().one()
    assert row["passed"] and row["read_only_since"] is not None
    assert row["read_only_since"].isoformat() == stamp["at"]
    # The revision alone writes the reconciliation and leaves the stamp empty.
    with engine.begin() as conn:
        conn.execute(text("UPDATE audit_legacy_copy SET read_only_since = NULL"))
        copy_revision._store(conn, copy_revision.copy(conn))
        assert (
            conn.execute(text("SELECT read_only_since FROM audit_legacy_copy")).scalar()
            is None
        )

    # A stamp left unset (a crash after the revision committed) is set on restart.
    code, again = _run(engine, tmp_path)
    assert (code, again["outcome"]) == (command.EXIT_OK, "restart")
    assert again["legacy_audit_read_only"]["set"], again


def test_deleting_an_investigation_keeps_its_log_rows(copied):
    engine = copied[0]
    with pytest.raises(Exception, match="investigation_logs_investigation_id_fkey"):
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM investigations WHERE investigation_id = 'INV-1'")
            )
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM investigation_logs")).scalar() == 1
        )
