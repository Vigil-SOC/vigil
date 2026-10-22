"""``python -m core.upgrade`` adopting a 0.7.0 or 0.8.0 install (core/upgrade/adopt.py).

Each tag's clean install is committed as ``fixtures/adoption/<tag>.sql``
(``build.py`` there rebuilds it). The drifted variants are that v0.7.0 dump
with one catalogue entry undone: a Helm install, objects owned by
``vigil_app`` (or no ``vigil_app`` role), and a legacy role a user holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from core.storage.connection import _stamp_schema_version
from core.upgrade import command
from core.upgrade.adopt import read_stamp
from core.upgrade.schema import compare, schema_state
from tests.integration.test_upgrade_command import (
    HEAD,
    REVISIONS,
    _app_can_move_revision,
    _rows,
    _run,
    _scratch,
    _version,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adoption"
TAGS = ("v0.7.0", "v0.8.0")


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """An empty database upgraded to head: its schema and seeded rows.

    Adoption stamps 0001_baseline (checked against baseline_schema.json) and the
    later revisions follow, so an adopted install must end here.
    """
    with _scratch(False) as engine:
        code, report = _run(
            engine, tmp_path_factory.mktemp("fresh"), restore_point="rp"
        )
        assert code == command.EXIT_OK, report
        with engine.connect() as conn:
            return schema_state(conn), _rows(conn)


@pytest.fixture
def scratch():
    with _scratch(False) as engine:
        yield engine


def _exec(engine, sql: str) -> None:
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute("RESET ALL")  # the dump empties search_path
        raw.commit()
    finally:
        raw.close()


def _load(engine, tag: str) -> None:
    with engine.connect() as conn:
        owner = conn.execute(text("SELECT quote_ident(current_user)")).scalar_one()
    _exec(
        engine,
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_app') "
        "THEN CREATE ROLE vigil_app NOSUPERUSER LOGIN; END IF; END $$;",
    )
    _exec(
        engine, (FIXTURES / f"{tag}.sql").read_text().replace("__VIGIL_OWNER__", owner)
    )


def _snapshot(engine) -> tuple[dict, dict]:
    """The schema and every row of every table."""
    with engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        rows = {
            t: sorted(
                json.dumps(r, sort_keys=True, default=str)
                for (r,) in conn.execute(
                    text(f'SELECT to_jsonb(t) FROM public."{t}" t')
                )
            )
            for t in tables
        }
        return schema_state(conn), rows


def _operator_rows(rows: dict) -> dict:
    """Seeded rows, less bifrost.virtual_keys: 0.7 wrote budget keys into it
    (16_llm_budgets.sql), and adoption does not rewrite operator rows."""
    return {
        table: [r for r in found if '"key": "bifrost.virtual_keys"' not in r]
        for table, found in rows.items()
    }


def _adopted(engine, baseline, tag: str = "v0.7.0", unowned=()) -> dict:
    with engine.connect() as conn:
        diff = compare(baseline[0], schema_state(conn))
        assert [e for e in diff if e["object"] not in unowned] == []
        assert read_stamp(conn) == tag.lstrip("v"), "adoption wrote schema_version"
        assert not _app_can_move_revision(conn), "the app role can move the revision"
        return _operator_rows(_rows(conn))


@pytest.mark.parametrize("tag", TAGS)
def test_clean_release_adopts_to_the_baseline(baseline, scratch, tmp_path, tag):
    _load(scratch, tag)
    before = _snapshot(scratch)

    code, report = _run(scratch, tmp_path, check=True)
    assert (code, report["verdict"]) == (
        command.EXIT_REFUSED,
        command.NEEDS_RESTORE_POINT,
    )
    assert f"adoption from {tag[1:]} would run" in report["reason"]
    assert report["drift"] == [] and report["pending"] == REVISIONS
    assert report["adoption"]["from"] == tag[1:]
    assert _snapshot(scratch) == before, "--check changed the database"
    assert not (tmp_path / "upgrade").exists(), "--check wrote the upgrade directory"

    code, outcome = _run(scratch, tmp_path, restore_point="rp-1")
    assert (code, outcome["outcome"]) == (command.EXIT_OK, "applied"), outcome
    assert _version(scratch) == HEAD
    assert _adopted(scratch, baseline, tag) == _operator_rows(baseline[1])

    code, again = _run(scratch, tmp_path)
    assert (code, again["outcome"]) == (command.EXIT_OK, "restart")


def _helm(engine) -> None:
    """What the chart's db-init left: the EXEMPT files and the 0.6.0 gap
    unapplied, and grants to deeptempo from the 0.7 copies of the init files."""
    with engine.connect() as conn:
        owner = conn.execute(text("SELECT current_user")).scalar_one()
    if owner != "deeptempo":
        _exec(
            engine,
            """
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'deeptempo')
              THEN CREATE ROLE deeptempo NOLOGIN; END IF;
            END $$;
            GRANT SELECT, INSERT, UPDATE, DELETE ON system_config TO deeptempo;
            GRANT USAGE, SELECT ON SEQUENCE config_audit_log_id_seq TO deeptempo;
        """,
        )
    _exec(
        engine,
        """
        DELETE FROM sla_policies; DELETE FROM case_templates;
        ALTER TABLE case_closure_info DROP CONSTRAINT case_closure_info_closed_by_kind_check;
        DROP INDEX idx_case_findings_finding;
        CREATE TABLE skills (skill_id varchar(50) PRIMARY KEY, name text);
        ALTER TABLE workflow_runs ADD COLUMN skill_tools_available jsonb;
        DROP TABLE mcp_credentials;
        CREATE TABLE _vigil_schema_versions (filename text PRIMARY KEY);
    """,
    )


def _app_owned(engine) -> None:
    """create_all run as vigil_app before 30_vigil_app_role.sql granted anything."""
    _exec(
        engine,
        """
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM vigil_app;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM vigil_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM vigil_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM vigil_app;
        ALTER TABLE findings OWNER TO vigil_app;
        ALTER TABLE mcp_credentials OWNER TO vigil_app;
        ALTER TABLE chat_messages OWNER TO vigil_app;
    """,
    )


def _no_app_role(engine) -> None:
    """vigil_app never created. The role is cluster-wide, so only where nothing
    else on the server depends on it."""
    _exec(engine, "DROP OWNED BY vigil_app")
    try:
        _exec(engine, "DROP ROLE vigil_app")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"vigil_app is in use elsewhere on this server: {e}")


def _legacy_role(engine) -> None:
    """The rows an older migrate_schema.py seeded; a user still holds 'analyst'."""
    _exec(
        engine,
        """
        DELETE FROM roles WHERE role_id = 'role-viewer';
        INSERT INTO roles (role_id, name, description, is_system_role) VALUES
            ('admin', 'Administrator', 'Full system access', true),
            ('analyst', 'Security Analyst', 'Can manage cases and findings', true),
            ('viewer', 'Viewer', 'Read-only access', true);
        INSERT INTO users (user_id, username, email, password_hash, full_name, role_id)
        VALUES ('u-1', 'ana', 'ana@example.com', 'x', 'Ana', 'analyst');
    """,
    )


@pytest.mark.parametrize(
    "drift", [_helm, _app_owned, _no_app_role], ids=["helm", "app-owned", "no-app-role"]
)
def test_known_drift_is_repaired(baseline, scratch, tmp_path, drift):
    _load(scratch, "v0.7.0")
    drift(scratch)

    code, report = _run(scratch, tmp_path, check=True)
    assert report["verdict"] == command.NEEDS_RESTORE_POINT, report
    assert report["drift"] == []
    code, outcome = _run(scratch, tmp_path, restore_point="rp-1")
    assert code == command.EXIT_OK, outcome

    # Not Vigil's: listed, left alone, and no reason to refuse.
    unowned = [e["object"] for e in outcome["adoption"]["unowned"]]
    assert ("relation _vigil_schema_versions" in unowned) == (drift is _helm)
    assert _adopted(scratch, baseline, unowned=unowned) == _operator_rows(baseline[1])
    with scratch.connect() as conn:
        assert not conn.execute(
            text(
                "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relowner = 'vigil_app'::regrole"
            )
        ).scalar()


def test_a_held_legacy_role_is_kept_and_named(baseline, scratch, tmp_path):
    _load(scratch, "v0.7.0")
    _legacy_role(scratch)

    code, report = _run(scratch, tmp_path, check=True)
    assert report["adoption"]["kept_roles"] == {"analyst": 1}
    code, outcome = _run(scratch, tmp_path, restore_point="rp-1")
    assert code == command.EXIT_OK, outcome
    assert outcome["adoption"]["kept_roles"] == {"analyst": 1}
    assert outcome["adoption"]["removed_roles"] == ["admin", "viewer"]

    _adopted(scratch, baseline)
    with scratch.connect() as conn:
        roles = set(conn.execute(text("SELECT role_id FROM roles")).scalars())
    assert "analyst" in roles and not roles & {"admin", "viewer"}


def test_unknown_drift_refuses_and_writes_nothing(scratch, tmp_path):
    _load(scratch, "v0.7.0")
    _helm(scratch)  # known drift alongside does not change the verdict
    _exec(
        scratch,
        """
        ALTER TABLE cases ADD COLUMN operator_note text;
        CREATE TABLE operator_notes (id int);
    """,
    )
    before = _snapshot(scratch)

    code, report = _run(scratch, tmp_path, check=True)
    assert (code, report["verdict"]) == (command.EXIT_REFUSED, command.REFUSE)
    assert "There is no override" in report["reason"]
    assert [e["object"] for e in report["drift"]] == ["column cases.operator_note"]
    assert "relation operator_notes" in [
        e["object"] for e in report["adoption"]["unowned"]
    ]

    # Neither a restore point nor an acknowledgement gets past it.
    for kw in (
        {"restore_point": "rp-1"},
        {"ack_version": command.__version__, "ack_reason": "own backup"},
    ):
        code, outcome = _run(scratch, tmp_path, **kw)
        assert (code, outcome["outcome"]) == (command.EXIT_REFUSED, "refused")
        assert outcome["reason"] == report["reason"]
        assert outcome["drift"] == report["drift"]
    assert _version(scratch) is None
    assert _snapshot(scratch) == before


@pytest.mark.parametrize("stamp", [None, "0.6.0", "0.8.1-dev"])
def test_a_stamp_outside_the_window_says_install_0_7_first(scratch, tmp_path, stamp):
    _load(scratch, "v0.7.0")
    _exec(
        scratch,
        (
            "DELETE FROM system_config WHERE key = 'schema_version'"
            if stamp is None
            else "UPDATE system_config SET value = jsonb_build_object('version', "
            f"'{stamp}') WHERE key = 'schema_version'"
        ),
    )
    code, report = _run(scratch, tmp_path, check=True)
    assert report["verdict"] == command.REFUSE
    assert "install a 0.7.x release first" in report["reason"]


def test_a_restamp_by_an_older_release_says_restore_first(baseline, scratch, tmp_path):
    _load(scratch, "v0.7.0")
    assert _run(scratch, tmp_path, restore_point="rp-1")[0] == command.EXIT_OK
    # This image's own startup stamp leaves an adopted database alone.
    _stamp_schema_version(scratch)
    assert _run(scratch, tmp_path)[1]["outcome"] == "restart"
    # What 0.7.0's init_database() does when it starts against the database.
    _exec(
        scratch,
        'UPDATE system_config SET value = \'{"version": "0.7.0"}\', '
        "updated_at = now() WHERE key = 'schema_version'",
    )
    code, report = _run(scratch, tmp_path, check=True)
    assert report["verdict"] == command.REFUSE
    assert "restore first" in report["reason"]
    assert report["drift"] == []
