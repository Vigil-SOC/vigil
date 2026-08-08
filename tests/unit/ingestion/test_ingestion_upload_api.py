from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
for p in (str(_REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.api.routers import ingestion as ingestion_api  # noqa: E402
from core.ingestion import ingestion_service  # noqa: E402
from core.ingestion.ingestion_jobs import IngestionJobRegistry  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeService:
    gate: threading.Event = None
    result: dict = {}
    calls: list = []

    def __init__(self):
        self.stats = {
            "findings_total": 0,
            "findings_imported": 0,
            "findings_skipped": 0,
            "findings_errors": 0,
            "cases_total": 0,
            "cases_imported": 0,
            "cases_skipped": 0,
            "cases_errors": 0,
        }

    def _ingest_file_by_format(self, file_path, fmt, data_type="finding"):
        type(self).calls.append((fmt, data_type))
        if type(self).gate is not None:
            type(self).gate.wait(timeout=5)
        self.stats.update(type(self).result)
        return self.stats


@pytest.fixture
def client(monkeypatch):
    _FakeService.gate = None
    _FakeService.result = {"findings_total": 2, "findings_imported": 2}
    _FakeService.calls = []

    registry = IngestionJobRegistry()
    monkeypatch.setattr(ingestion_api, "get_job_registry", lambda: registry)
    monkeypatch.setattr(ingestion_service, "IngestionService", lambda: _FakeService())

    app = FastAPI()
    app.include_router(ingestion_api.router, prefix="/api/ingest")
    with TestClient(app) as test_client:
        yield test_client


def _upload(client, name="export.parquet", body=b"payload", data_type="finding"):
    return client.post(
        "/api/ingest/upload",
        files={"file": (name, body, "application/octet-stream")},
        data={"data_type": data_type},
    )


def _await_terminal(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/ingest/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def test_upload_is_accepted_without_waiting_for_the_ingest(client):
    response = _upload(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["filename"] == "export.parquet"
    assert body["format"] == "parquet"
    assert body["determinate"] is True


def test_the_job_reaches_success_and_reports_its_stats(client):
    job_id = _upload(client).json()["job_id"]

    job = _await_terminal(client, job_id)

    assert job["status"] == "succeeded"
    assert job["message"] == "Imported 2 findings"
    assert job["processed"] == 2
    assert job["total"] == 2


def test_format_is_detected_from_the_extension(client):
    assert _upload(client, name="a.csv").json()["format"] == "csv"
    assert _upload(client, name="a.ndjson").json()["format"] == "jsonl"


def test_an_unknown_extension_is_rejected(client):
    response = _upload(client, name="notes.txt")

    assert response.status_code == 400
    assert "Unable to detect file format" in response.json()["detail"]


def test_an_unknown_data_type_is_rejected(client):
    response = _upload(client, data_type="widget")

    assert response.status_code == 400
    assert "data_type" in response.json()["detail"]


def test_an_oversized_upload_is_rejected_before_a_job_is_created(client, monkeypatch):
    monkeypatch.setattr(ingestion_api, "MAX_UPLOAD_SIZE_BYTES", 8)

    response = _upload(client, body=b"x" * 64)

    assert response.status_code == 413
    assert client.get("/api/ingest/jobs").json() == []


def test_a_second_upload_is_refused_while_one_is_running(client):
    _FakeService.gate = threading.Event()
    try:
        first = _upload(client, name="first.parquet")
        assert first.status_code == 202

        second = _upload(client, name="second.parquet")

        assert second.status_code == 409
        assert "first.parquet" in second.json()["detail"]
    finally:
        _FakeService.gate.set()
    _await_terminal(client, first.json()["job_id"])


def test_the_slot_is_released_once_the_running_job_finishes(client):
    first = _upload(client, name="first.parquet")
    _await_terminal(client, first.json()["job_id"])

    assert _upload(client, name="second.parquet").status_code == 202


def test_jobs_are_listed_newest_first_for_re_attach(client):
    first = _upload(client, name="first.parquet").json()["job_id"]
    _await_terminal(client, first)
    second = _upload(client, name="second.parquet").json()["job_id"]
    _await_terminal(client, second)

    listed = client.get("/api/ingest/jobs").json()

    assert [j["filename"] for j in listed] == ["second.parquet", "first.parquet"]


def test_polling_an_unknown_job_is_a_404(client):
    response = client.get("/api/ingest/jobs/ing-missing")

    assert response.status_code == 404


def test_the_declared_data_type_reaches_the_service(client):
    job_id = _upload(client, name="cases.jsonl", data_type="case").json()["job_id"]
    _await_terminal(client, job_id)

    assert _FakeService.calls == [("jsonl", "case")]
