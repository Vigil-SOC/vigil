from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.ingestion_service import (  # noqa: E402
    ID_HASH_WIDTH,
    IngestionService,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    return IngestionService()


def _parquet_row(**overrides):
    row = {
        "event_start_time": 1753000000000,
        "event_end_time": 1753000060000,
        "focal_ip": "10.0.0.1",
        "engaged_ip": "10.0.0.2",
        "embedding": [0.1, 0.2, 0.3],
    }
    row.update(overrides)
    return row


def _csv_row(**overrides):
    row = {
        "event_start": "2026-07-21T12:00:00Z",
        "event_end": "2026-07-21T12:01:00Z",
        "IP1": "10.0.0.1",
        "IP2": "10.0.0.2",
        "mitre_tactic": "Command and Control",
        "created_at": "2026-07-21T12:02:00Z",
    }
    row.update(overrides)
    return row


def _id(service, row):
    return service._parquet_row_to_finding(row)["finding_id"]


# --- the reported bug -----------------------------------------------------


def test_parquet_rows_without_sequence_id_stay_distinct(service):
    """L7-style parquet with no sequence_id column used to collapse every row
    onto sha256('') and report the rest as duplicates."""
    rows = [
        _parquet_row(focal_ip=f"10.0.0.{i}", embedding=[float(i)]) for i in range(200)
    ]

    ids = [_id(service, row) for row in rows]

    assert len(set(ids)) == 200


def test_the_empty_string_hash_is_no_longer_produced(service):
    import hashlib

    empty = hashlib.sha256(b"").hexdigest()[:ID_HASH_WIDTH]

    assert empty not in _id(service, _parquet_row())


def test_tempo_csv_rows_without_sequence_id_stay_distinct(service):
    rows = [_csv_row(IP1=f"10.0.0.{i}") for i in range(50)]

    ids = [service._tempo_csv_row_to_finding(row)["finding_id"] for row in rows]

    assert len(set(ids)) == 50


# --- identity is content-stable so re-ingest still dedupes ----------------


def test_the_same_row_yields_the_same_id(service):
    assert _id(service, _parquet_row()) == _id(service, _parquet_row())


def test_a_fresh_service_yields_the_same_id():
    """Re-uploading a file must dedupe, so ids cannot depend on run state."""
    assert _id(IngestionService(), _parquet_row()) == _id(
        IngestionService(), _parquet_row()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("focal_ip", "10.9.9.9"),
        ("engaged_ip", "10.9.9.9"),
        ("embedding", [9.9]),
        ("event_end_time", 1753000999000),
    ],
)
def test_differing_in_any_identity_column_changes_the_id(service, field, value):
    assert _id(service, _parquet_row()) != _id(service, _parquet_row(**{field: value}))


# --- sequence_id still wins when present ---------------------------------


def test_sequence_id_drives_the_id_when_present(service):
    a = _id(service, _parquet_row(sequence_id="seq-1"))
    b = _id(service, _parquet_row(sequence_id="seq-2"))
    assert a != b

    # Same sequence_id, different content -> same id, since the source
    # declared the identity itself.
    c = _id(service, _parquet_row(sequence_id="seq-1", focal_ip="10.9.9.9"))
    assert a == c


def test_a_blank_sequence_id_falls_back_instead_of_colliding(service):
    """An empty or null cell is not an identity."""
    blank = _id(service, _parquet_row(sequence_id=""))
    null = _id(service, _parquet_row(sequence_id=None, focal_ip="10.9.9.9"))

    assert blank != null


def test_tempo_csv_separates_a_sequence_across_attack_clusters(service):
    a = service._tempo_csv_row_to_finding(_csv_row(sequence_id="s1", attack_id="a1"))
    b = service._tempo_csv_row_to_finding(_csv_row(sequence_id="s1", attack_id="a2"))

    assert a["finding_id"] != b["finding_id"]


# --- hash width -----------------------------------------------------------


def test_the_id_hash_is_64_bits(service):
    assert ID_HASH_WIDTH == 16
    assert len(_id(service, _parquet_row()).rsplit("-", 1)[1]) == 16


# --- the missing column is reported --------------------------------------


def test_a_missing_sequence_id_column_warns_once_not_per_row(service, caplog):
    with caplog.at_level("WARNING"):
        for i in range(20):
            _id(service, _parquet_row(focal_ip=f"10.0.0.{i}"))

    warnings = [r for r in caplog.records if "No 'sequence_id' column" in r.message]
    assert len(warnings) == 1


def test_no_warning_when_sequence_id_is_present(service, caplog):
    with caplog.at_level("WARNING"):
        _id(service, _parquet_row(sequence_id="seq-1"))

    assert not [r for r in caplog.records if "No 'sequence_id' column" in r.message]
