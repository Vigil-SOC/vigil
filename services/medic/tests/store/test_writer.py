"""S2: the single writer appends through the contract's chain code, and verify() finds tampering.

K1 §6 G2: "append-only, hash-chained log". C4 §6.3 / §6.7: one writer, WAL, durable commits.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from services.medic.contracts import decision_chain
from services.medic.store import (
    RecordRefused,
    StoreBusy,
    StoreLocked,
    StoreRefused,
    open_writer,
    verify_store,
)
from services.medic.tests.store.chains import (
    FIXTURE_INSTANCE,
    INVALID,
    VALID,
    draft,
    load,
    raw,
    seed_instance,
    stored,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "medic"
    seed_instance(d)
    return d


def _write_day(data_dir: Path, records: list[dict]) -> None:
    with open_writer(data_dir) as w:
        for rec in records:
            w.append(draft(rec))


# --- valid fixtures append and verify --------------------------------------------------


def test_the_contract_module_is_the_one_implementation() -> None:
    # S2 PROVISIONAL S2-2: imported from services.medic.contracts, not forked.
    from services.medic.store import writer

    assert writer.decision_chain is decision_chain


def test_pilot_day_appends_byte_identical_to_the_contract(data_dir: Path) -> None:
    day = load("chain-01-pilot-day.jsonl")
    with open_writer(data_dir) as w:
        for rec in day:
            sealed = w.append(draft(rec))
            assert sealed == rec, rec["seq"]
    assert stored(data_dir) == day
    report = verify_store(data_dir)
    assert report.ok and report.count == len(day)
    assert report.head_seq == day[-1]["seq"] and report.head_hash == day[-1]["hash"]


def test_a_store_reset_starts_a_fresh_store_after_the_old_head(data_dir: Path) -> None:
    reset, opened = load("chain-03-store-reset.jsonl")
    with open_writer(data_dir) as w:
        first = w.reset(
            at=reset["at"],
            previous_head=reset["body"]["previous_head"],
            moved_aside=reset["body"]["moved_aside"],
        )
        assert first == reset
        assert first["body"]["instance_id"] == FIXTURE_INSTANCE
        assert w.append(draft(opened)) == opened
    assert verify_store(data_dir).ok


def test_a_purged_store_verifies_from_its_anchor_and_keeps_appending(
    data_dir: Path,
) -> None:
    # Purge itself is G3's. This loads the rows a purge leaves behind, the way it would.
    purged = load("chain-02-after-purge.jsonl")
    with open_writer(data_dir):
        pass
    conn = raw(data_dir)
    conn.executemany(
        "INSERT INTO records (seq, hash, record) VALUES (?, ?, ?)",
        [(r["seq"], r["hash"], json.dumps(r)) for r in purged],
    )
    conn.close()
    assert verify_store(data_dir).ok
    feedback = deepcopy(next(r for r in purged if r["type"] == "feedback"))
    feedback["at"] = "2026-12-11T10:00:00Z"
    with open_writer(data_dir) as w:
        sealed = w.append(draft(feedback))
    assert sealed["seq"] == purged[-1]["seq"] + 1
    assert sealed["prev"] == purged[-1]["hash"]
    assert verify_store(data_dir).ok


def test_valid_fixture_list_is_the_three_the_tests_cover() -> None:
    assert [p.name for p in VALID] == [
        "chain-01-pilot-day.jsonl",
        "chain-02-after-purge.jsonl",
        "chain-03-store-reset.jsonl",
    ]


# --- invalid fixtures are refused ----------------------------------------------------------

# Every invalid fixture breaks the schema, so the writer names E-SCHEMA and the field.
# bad-prev-hash is different: a caller can't set seq, prev or hash at all
# (E-ENVELOPE names the first one it carries).
EXPECTED = {
    "bad-prev-hash": ("E-ENVELOPE", "seq"),
    "excerpt-too-long": ("E-SCHEMA", "$.body.evidence[0].excerpt.text"),
    "excerpt-without-redaction": ("E-SCHEMA", "$.body.evidence[0].excerpt"),
    "feedback-admin-free-text": ("E-SCHEMA", "$.body.admin"),
    "feedback-suggested-lane-on-agree": ("E-SCHEMA", "$.body"),
    "float-value": ("E-SCHEMA", "$.body.evidence[0].value"),
    "free-text-field": ("E-SCHEMA", "$.body"),
    "instance-id-hostname": ("E-SCHEMA", "$.body.instance_id"),
    "lane-four": ("E-SCHEMA", "$.body.lane"),
    "missing-would-have": ("E-SCHEMA", "$.body"),
    "pack-event-admin-import-without-by": ("E-SCHEMA", "$.body"),
    "pack-event-skip-without-rule": ("E-SCHEMA", "$.body"),
    "pack-event-unknown-event": ("E-SCHEMA", "$.body"),
    "templated-group-value": ("E-SCHEMA", "$.body.group[0].value"),
    "too-much-evidence": ("E-SCHEMA", "$.body.evidence"),
    "vigil-lane-null": ("E-SCHEMA", "$.body"),
}


def test_every_invalid_fixture_has_an_expected_error() -> None:
    assert sorted(EXPECTED) == [p.stem for p in INVALID]


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_is_refused_with_the_expected_error(
    data_dir: Path, path: Path
) -> None:
    doc = json.loads(path.read_text())
    doc.pop("$comment")
    code, where = EXPECTED[path.stem]
    with open_writer(data_dir) as w:
        with pytest.raises(RecordRefused) as err:
            w.append(doc if code == "E-ENVELOPE" else draft(doc))
        assert err.value.code == code
        assert err.value.where.startswith(where), err.value.where
    assert stored(data_dir) == [], "a refused record must not be written"


@pytest.mark.parametrize("bad", [float("nan"), {1, 2}])
def test_a_value_that_isnt_canonical_json_is_refused(data_dir: Path, bad) -> None:
    rec = draft(load("chain-01-pilot-day.jsonl")[1])
    rec["body"]["value"] = bad
    with open_writer(data_dir) as w, pytest.raises(RecordRefused) as err:
        w.append(rec)
    assert err.value.code == "E-SCHEMA"


def test_a_record_naming_another_instance_is_refused(data_dir: Path) -> None:
    opened = draft(load("chain-01-pilot-day.jsonl")[0])
    opened["body"]["instance_id"] = "mi_0000000000000001"
    with open_writer(data_dir) as w, pytest.raises(RecordRefused) as err:
        w.append(opened)
    assert err.value.code == "E-INSTANCE"


def test_store_reset_only_through_reset_and_only_first(data_dir: Path) -> None:
    reset, opened = load("chain-03-store-reset.jsonl")
    with open_writer(data_dir) as w:
        with pytest.raises(RecordRefused) as err:
            w.append(draft(reset))
        assert err.value.code == "E-TYPE"
        w.append(draft(opened))
        with pytest.raises(RecordRefused) as err:
            w.reset(at=reset["at"], previous_head=None)
        assert err.value.code == "E-TYPE"


def test_a_failed_append_leaves_the_chain_where_it_was(data_dir: Path) -> None:
    day = load("chain-01-pilot-day.jsonl")
    with open_writer(data_dir) as w:
        w.append(draft(day[0]))
        bad = draft(day[1])
        bad["body"]["value"] = "maybe"
        with pytest.raises(RecordRefused):
            w.append(bad)
        assert w.append(draft(day[1])) == day[1]


def test_records_cannot_be_updated_in_place(data_dir: Path) -> None:
    _write_day(data_dir, load("chain-01-pilot-day.jsonl")[:2])
    with (
        sqlite3.connect(data_dir / "medic.db") as conn,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        conn.execute("UPDATE records SET record = '{}' WHERE seq = 0")


# --- tampering: verify() names the first bad seq --------------------------------------------


@pytest.fixture
def day_store(data_dir: Path) -> Path:
    _write_day(data_dir, load("chain-01-pilot-day.jsonl"))
    return data_dir


def _edit(data_dir: Path, seq: int, change) -> None:
    conn = raw(data_dir)
    (text,) = conn.execute(
        "SELECT record FROM records WHERE seq = ?", (seq,)
    ).fetchone()
    rec = json.loads(text)
    change(rec)
    conn.execute("UPDATE records SET record = ? WHERE seq = ?", (json.dumps(rec), seq))
    conn.close()


def _broken(data_dir: Path) -> tuple[str, int]:
    report = verify_store(data_dir)
    assert not report.ok
    return report.code, report.first_bad_seq


def test_editing_a_payload_is_named(day_store: Path) -> None:
    _edit(day_store, 5, lambda r: r["body"].__setitem__("value", "false_alarm"))
    assert _broken(day_store) == ("E-HASH", 5)


def test_editing_and_rehashing_breaks_the_next_link(day_store: Path) -> None:
    def change(r: dict) -> None:
        r["body"]["value"] = "false_alarm"  # flip an admin's agree (seq 1 is feedback)
        r["hash"] = decision_chain.record_hash(r)

    _edit(day_store, 1, change)
    # The hash column still holds the old hash, so the row is named first.
    assert _broken(day_store) == ("E-HASH", 1)
    conn = raw(day_store)
    (text,) = conn.execute("SELECT record FROM records WHERE seq = 1").fetchone()
    conn.execute(
        "UPDATE records SET hash = ? WHERE seq = 1", (json.loads(text)["hash"],)
    )
    conn.close()
    assert _broken(day_store) == ("E-LINK", 2)


def test_deleting_a_row_is_named(day_store: Path) -> None:
    conn = raw(day_store)
    conn.execute("DELETE FROM records WHERE seq = 3")
    conn.close()
    assert _broken(day_store) == ("E-SEQ", 4)


def test_deleting_the_first_row_is_named(day_store: Path) -> None:
    conn = raw(day_store)
    conn.execute("DELETE FROM records WHERE seq = 0")
    conn.close()
    assert _broken(day_store) == ("E-UNANCHORED", 1)


def test_reordering_two_rows_is_named(day_store: Path) -> None:
    conn = raw(day_store)
    rows = dict(conn.execute("SELECT seq, record FROM records WHERE seq IN (2, 3)"))
    conn.execute("UPDATE records SET record = ? WHERE seq = 2", (rows[3],))
    conn.execute("UPDATE records SET record = ? WHERE seq = 3", (rows[2],))
    conn.close()
    assert _broken(day_store) == ("E-SEQ", 2)


def test_changing_a_prev_is_named(day_store: Path) -> None:
    _edit(day_store, 7, lambda r: r.__setitem__("prev", "a" * 64))
    assert _broken(day_store) == ("E-HASH", 7)


def test_changing_a_prev_and_rehashing_is_named(day_store: Path) -> None:
    def change(r: dict) -> None:
        r["prev"] = "a" * 64
        r["hash"] = decision_chain.record_hash(r)

    _edit(day_store, 7, change)
    conn = raw(day_store)
    (text,) = conn.execute("SELECT record FROM records WHERE seq = 7").fetchone()
    conn.execute(
        "UPDATE records SET hash = ? WHERE seq = 7", (json.loads(text)["hash"],)
    )
    conn.close()
    assert _broken(day_store) == ("E-LINK", 7)


def test_an_unreadable_row_is_named(day_store: Path) -> None:
    conn = raw(day_store)
    conn.execute("UPDATE records SET record = 'not json' WHERE seq = 9")
    conn.close()
    assert _broken(day_store) == ("E-PARSE", 9)


def test_the_writer_refuses_to_extend_a_broken_chain(day_store: Path) -> None:
    _edit(day_store, 5, lambda r: r["body"].__setitem__("value", "false_alarm"))
    with pytest.raises(StoreRefused, match="seq 5"):
        open_writer(day_store)


# --- restart and the single writer ----------------------------------------------------------


def test_the_chain_continues_after_restart(data_dir: Path) -> None:
    day = load("chain-01-pilot-day.jsonl")
    _write_day(data_dir, day[:6])
    _write_day(data_dir, day[6:])
    assert stored(data_dir) == day
    assert verify_store(data_dir).head_hash == day[-1]["hash"]


def test_a_second_writer_is_refused(data_dir: Path) -> None:
    with open_writer(data_dir), pytest.raises(StoreLocked, match="another Medic"):
        open_writer(data_dir)
    with open_writer(data_dir):
        pass  # released on close


def test_a_second_writer_in_another_process_is_refused(data_dir: Path) -> None:
    code = (
        "import sys; from services.medic.store import open_writer, StoreLocked\n"
        "try:\n    open_writer(__import__('pathlib').Path(sys.argv[1]))\n"
        "except StoreLocked as e:\n    print(e); sys.exit(3)\n"
    )
    with open_writer(data_dir):
        out = subprocess.run(
            [sys.executable, "-c", code, str(data_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert out.returncode == 3, out.stdout + out.stderr


def test_a_rogue_connection_cannot_interleave_with_an_append(data_dir: Path) -> None:
    day = load("chain-01-pilot-day.jsonl")
    with open_writer(data_dir, busy_timeout_ms=50) as w:
        w.append(draft(day[0]))
        rogue = sqlite3.connect(data_dir / "medic.db", isolation_level=None)
        rogue.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreBusy):
            w.append(draft(day[1]))
        rogue.rollback()
        rogue.close()
        assert w.append(draft(day[1])) == day[1]


def test_readers_are_separate_read_only_connections(data_dir: Path) -> None:
    from services.medic.store import open_reader

    day = load("chain-01-pilot-day.jsonl")
    with open_writer(data_dir) as w:
        w.append(draft(day[0]))
        with open_reader(data_dir) as reader:
            assert reader.execute("SELECT count(*) FROM records").fetchone() == (1,)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("DELETE FROM records")
        w.append(draft(day[1]))


def test_durability_settings_on_the_writer_connection(data_dir: Path) -> None:
    # C4 §6.3: WAL, synchronous=FULL, incremental auto-vacuum set before the first table.
    with open_writer(data_dir) as w:
        pragma = w.pragmas()
    assert pragma["journal_mode"] == "wal"
    assert pragma["synchronous"] == 2  # FULL
    assert pragma["auto_vacuum"] == 2  # INCREMENTAL
    assert pragma["fullfsync"] == 1


def test_appending_after_close_is_refused(data_dir: Path) -> None:
    w = open_writer(data_dir)
    w.close()
    with pytest.raises(RecordRefused) as err:
        w.append(draft(load("chain-01-pilot-day.jsonl")[0]))
    assert err.value.code == "E-CLOSED"


# --- anyone can verify: the CLI ---------------------------------------------------------------


def test_verify_cli_reports_ok_and_broken(day_store: Path, capsys) -> None:
    from services.medic.store.__main__ import main

    assert main(["verify", "--data-dir", str(day_store)]) == 0
    assert "ok: 15 records" in capsys.readouterr().out
    _edit(day_store, 4, lambda r: r["body"].__setitem__("x", 1))
    assert main(["verify", "--data-dir", str(day_store)]) == 1
    assert "E-HASH at seq 4" in capsys.readouterr().out
