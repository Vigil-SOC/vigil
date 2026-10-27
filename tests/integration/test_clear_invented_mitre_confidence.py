"""Clear invented collector ATT&CK confidences (#3223).

The database is one ``python -m core.upgrade`` built. The clear then runs on
rows seeded into that database: the upgrade transaction has already committed,
so this calls ``clear`` itself.
"""

from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import text

from core.upgrade.command import PACKAGE_DIR
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

_PATH = PACKAGE_DIR / "versions" / "0015_clear_invented_mitre_conf.py"
_SPEC = importlib.util.spec_from_file_location("clear_invented_mitre_conf", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
revision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(revision)

# One prediction per finding, plus a second technique on the Darktrace row so
# rows_cleared counts predictions.
_CLEARED = (
    ("darktrace-constant", "darktrace", 0.7, "T1071"),
    ("darktrace-constant", "darktrace", 0.7, "T1059"),
    ("elastic-constant", "elastic", 0.9, "T1071"),
    ("crowdstrike-constant", "crowdstrike", 0.9, "T1071"),
    ("defender-constant", "microsoft_defender", 1.0, "T1071"),
)
_KEPT = (
    ("elastic-other", "elastic", 0.91, "T1071"),
    ("elastic-later", "elastic", 0.9, "T1071"),
    ("opensearch-other", "opensearch", 0.91, "T1071"),
    ("cloudflare", "cloudflare_cloudy", 1.0, "T1071"),
    ("flow", "flow", 1.0, "T1071"),
)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as scratch:
        code, report = _run(scratch, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == 0, report
        yield scratch


def _insert(
    conn, finding_id, data_source, confidence, technique, *, before: bool
) -> None:
    # The clear's cutoff is this transaction's start. The kept constant row is
    # created after that instant; every other row is strictly before it.
    # Event time is the other way around, and is not the cutoff.
    created = (
        "(transaction_timestamp() AT TIME ZONE 'UTC') - interval '1 second'"
        if before
        else "(transaction_timestamp() AT TIME ZONE 'UTC') + interval '1 second'"
    )
    event = (
        "(transaction_timestamp() AT TIME ZONE 'UTC') + interval '1 day'"
        if before
        else "(transaction_timestamp() AT TIME ZONE 'UTC') - interval '1 day'"
    )
    conn.execute(
        text(f"""
            INSERT INTO findings (
                finding_id, data_source, created_at, updated_at, timestamp
            )
            VALUES (
                :id, :source, {created},
                (transaction_timestamp() AT TIME ZONE 'UTC'),
                {event}
            )
            ON CONFLICT (finding_id) DO NOTHING
            """),
        {"id": finding_id, "source": data_source},
    )
    conn.execute(
        text("""
            INSERT INTO finding_mitre_predictions (
                finding_id, technique_id, confidence
            )
            VALUES (:id, :technique, :confidence)
            """),
        {"id": finding_id, "technique": technique, "confidence": confidence},
    )


def _seed(conn) -> None:
    for finding_id, data_source, confidence, technique in _CLEARED:
        _insert(conn, finding_id, data_source, confidence, technique, before=True)
    for finding_id, data_source, confidence, technique in _KEPT:
        _insert(
            conn,
            finding_id,
            data_source,
            confidence,
            technique,
            before=finding_id != "elastic-later",
        )


def _confidences(conn) -> dict[tuple[str, str], float | None]:
    rows = conn.execute(text("""
            SELECT finding_id, technique_id, confidence
            FROM finding_mitre_predictions
            ORDER BY finding_id, technique_id
            """)).all()
    return {
        (finding_id, technique_id): confidence
        for finding_id, technique_id, confidence in rows
    }


def _entries(conn) -> list[dict]:
    return list(conn.execute(text("""
                SELECT payload FROM audit_trail
                WHERE payload->>'action' = 'findings.technique_confidence_cleared'
                ORDER BY seq
                """)).scalars())


def test_the_comparison_binds_the_writer_float() -> None:
    sql = str(revision._CLEAR)
    assert "CAST(:constant AS double precision)" in sql
    assert "created_at < (transaction_timestamp() AT TIME ZONE 'UTC')" in sql
    for literal in ("0.7", "0.9", "1.0"):
        assert literal not in sql


def test_only_pre_upgrade_constants_clear_and_record_a_trail_entry(engine) -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        _seed(conn)
        revision.clear(conn)
        assert _confidences(conn) == {
            ("darktrace-constant", "T1059"): None,
            ("darktrace-constant", "T1071"): None,
            ("elastic-constant", "T1071"): None,
            ("elastic-other", "T1071"): 0.91,
            ("elastic-later", "T1071"): 0.9,
            ("crowdstrike-constant", "T1071"): None,
            ("defender-constant", "T1071"): None,
            ("opensearch-other", "T1071"): 0.91,
            ("cloudflare", "T1071"): 1.0,
            ("flow", "T1071"): 1.0,
        }
        entries = _entries(conn)
        assert [entry["data_source"] for entry in entries] == [
            "darktrace",
            "elastic",
            "crowdstrike",
            "microsoft_defender",
        ]
        assert [entry["constant"] for entry in entries] == [0.7, 0.9, 0.9, 1.0]
        assert [entry["rows_cleared"] for entry in entries] == [2, 1, 1, 1]
        assert {entry["revision"] for entry in entries} == {revision.revision}
        assert {entry["actor"] for entry in entries} == {"system"}
        assert {entry["action"] for entry in entries} == {
            "findings.technique_confidence_cleared"
        }
        transaction.rollback()

    with engine.connect() as conn:
        assert _confidences(conn) == {}
        assert _entries(conn) == []

    with engine.begin() as conn:
        _seed(conn)
        revision.clear(conn)
    with engine.connect() as conn:
        assert _confidences(conn)[("elastic-constant", "T1071")] is None
        assert _confidences(conn)[("elastic-later", "T1071")] == 0.9
        assert _confidences(conn)[("flow", "T1071")] == 1.0
        assert len(_entries(conn)) == 4
