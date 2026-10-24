"""An upload file that cannot be read fails its job and names the parse error."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.ingestion.ingestion_jobs import (  # noqa: E402
    FAILED,
    SUCCEEDED,
    IngestionJob,
    run_job,
)
from core.ingestion.ingestion_service import IngestionService  # noqa: E402

pytestmark = pytest.mark.unit


class _Db:
    def __init__(self):
        self.rows = []

    def bulk_create_findings(self, rows):
        self.rows.extend(rows)
        return {"imported": len(rows), "skipped": 0}


@pytest.fixture
def db(monkeypatch):
    db = _Db()
    init = IngestionService.__init__

    def with_db(self):
        init(self)
        self.use_database = True
        self.db_service = db

    monkeypatch.setattr(IngestionService, "__init__", with_db)
    return db


def _finding(i):
    return {
        "finding_id": f"f-{i}",
        "title": "t",
        "data_source": "x",
        "timestamp": "2026-01-01T00:00:00Z",
        "severity": "low",
    }


def _run(path, fmt, data_type="finding"):
    job = IngestionJob(path.name, fmt, data_type)
    run_job(job, path)
    return job


def test_truncated_json_past_one_batch_fails_with_the_parse_error(tmp_path, db):
    text = json.dumps([_finding(i) for i in range(1500)])
    path = tmp_path / "trunc.json"
    path.write_text(text[: len(text) * 3 // 4])

    job = _run(path, "json")

    assert job.status == FAILED
    assert "1 finding errors. First error: Could not read the JSON file:" in (
        job.message
    )
    assert len(db.rows) == len({r["finding_id"] for r in db.rows}) > 1000


def test_truncated_small_json_fails_with_the_parse_error(tmp_path, db):
    path = tmp_path / "trunc.json"
    path.write_text(json.dumps([_finding(i) for i in range(10)])[:-20])

    job = _run(path, "json")

    assert job.status == FAILED
    assert "Could not read the JSON file:" in job.message


def test_json_array_of_non_objects_fails_with_the_cause(tmp_path, db):
    path = tmp_path / "ints.json"
    path.write_text("[1, 2, 3]")

    job = _run(path, "json")

    assert job.status == FAILED
    assert "Could not read the JSON file:" in job.message


def test_corrupt_parquet_fails_with_the_parse_error(tmp_path, db):
    path = tmp_path / "bad.parquet"
    path.write_bytes(b"not a parquet file" * 10)

    job = _run(path, "parquet")

    assert job.status == FAILED
    assert "Could not read the Parquet file:" in job.message


def test_non_utf8_csv_fails_with_the_decode_error(tmp_path, db):
    path = tmp_path / "latin1.csv"
    path.write_bytes("finding_id,title\nf-1,caf\xe9\n".encode("latin-1"))

    job = _run(path, "csv")

    assert job.status == FAILED
    assert "Could not read the CSV file: 'utf-8' codec can't decode" in job.message


def test_csv_rows_read_before_a_decode_error_are_kept(tmp_path, db):
    rows = "".join(f"f-{i},{'t' * 20}\n" for i in range(1500)).encode()
    path = tmp_path / "late.csv"
    path.write_bytes(b"finding_id,title\n" + rows + b"f-x,caf\xe9\n")

    job = _run(path, "csv")

    assert job.status == FAILED
    imported = int(job.message.split()[1])
    assert imported == len(db.rows) > 1000


def test_unreadable_case_csv_counts_a_case_error(tmp_path, db):
    path = tmp_path / "latin1.csv"
    path.write_bytes("case_id,title\nc-1,caf\xe9\n".encode("latin-1"))

    job = _run(path, "csv", data_type="case")

    assert job.status == FAILED
    assert job.message.startswith("1 case errors. First error: Could not read")


def test_non_utf8_jsonl_fails_with_the_decode_error(tmp_path, db):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"finding_id":"f-1","title":"caf\xe9"}\n')

    job = _run(path, "jsonl")

    assert job.status == FAILED
    assert "Could not read the JSONL file:" in job.message


def test_json_with_nan_imports(tmp_path, db):
    finding = {**_finding(0), "anomaly_score": float("nan")}
    path = tmp_path / "nan.json"
    path.write_text(json.dumps([finding]))

    job = _run(path, "json")

    assert job.status == SUCCEEDED
    assert [r["finding_id"] for r in db.rows] == ["f-0"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_nan_after_the_first_batch_imports_each_finding_once(tmp_path, db, wrapped):
    findings = [_finding(i) for i in range(1500)]
    findings[1200]["anomaly_score"] = float("nan")
    path = tmp_path / "nan.json"
    path.write_text(json.dumps({"findings": findings} if wrapped else findings))

    job = _run(path, "json")

    assert job.status == SUCCEEDED
    assert job.message == "Imported 1500 findings"
    assert [r["finding_id"] for r in db.rows] == [f"f-{i}" for i in range(1500)]
