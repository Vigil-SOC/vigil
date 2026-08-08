from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.ingestion import ingestion_service  # noqa: E402
from core.ingestion.ingestion_jobs import (  # noqa: E402
    FAILED,
    RUNNING,
    SUCCEEDED,
    IngestionJob,
    IngestionJobConflict,
    IngestionJobRegistry,
    run_job,
    summarize_stats,
)

pytestmark = pytest.mark.unit


def _stats(**overrides):
    base = {
        "findings_total": 0,
        "findings_imported": 0,
        "findings_skipped": 0,
        "findings_errors": 0,
        "cases_total": 0,
        "cases_imported": 0,
        "cases_skipped": 0,
        "cases_errors": 0,
    }
    base.update(overrides)
    return base


class _FakeService:
    def __init__(self, result=None, raises=None, on_ingest=None):
        self.stats = _stats()
        self._result = result
        self._raises = raises
        self._on_ingest = on_ingest
        self.calls = []

    def _ingest_file_by_format(self, file_path, fmt, data_type="finding"):
        self.calls.append((Path(file_path), fmt, data_type))
        if self._raises is not None:
            raise self._raises
        if self._on_ingest is not None:
            self._on_ingest(self.stats)
        self.stats.update(self._result or {})
        return self.stats


@pytest.fixture
def spooled(tmp_path):
    path = tmp_path / "upload.parquet"
    path.write_bytes(b"payload")
    return path


def _patch_service(monkeypatch, service):
    monkeypatch.setattr(ingestion_service, "IngestionService", lambda: service)


# --- summarize_stats ------------------------------------------------------


def test_summarize_reports_success_for_a_pure_import():
    success, message = summarize_stats(_stats(findings_imported=3))
    assert success is True
    assert message == "Imported 3 findings"


def test_summarize_counts_all_duplicates_as_success():
    success, message = summarize_stats(_stats(findings_skipped=5))
    assert success is True
    assert message == "Skipped 5 duplicate findings"


def test_summarize_fails_when_any_row_errored():
    success, message = summarize_stats(_stats(findings_imported=2, findings_errors=1))
    assert success is False
    assert "1 finding errors" in message


def test_summarize_fails_on_an_empty_ingest():
    success, message = summarize_stats(_stats())
    assert success is False
    assert message == "No data imported"


# --- job lifecycle --------------------------------------------------------


def test_run_job_records_success_and_deletes_the_spooled_file(monkeypatch, spooled):
    _patch_service(monkeypatch, _FakeService(result={"findings_imported": 7}))
    job = IngestionJob("export.parquet", "parquet", "finding")

    run_job(job, spooled)

    assert job.status == SUCCEEDED
    assert job.message == "Imported 7 findings"
    assert job.error is None
    assert job.finished_at is not None
    assert not spooled.exists()


def test_run_job_records_failure_and_still_deletes_the_spooled_file(
    monkeypatch, spooled
):
    _patch_service(monkeypatch, _FakeService(raises=ValueError("bad parquet")))
    job = IngestionJob("export.parquet", "parquet", "finding")

    run_job(job, spooled)

    assert job.status == FAILED
    assert "bad parquet" in job.error
    assert not spooled.exists()


def test_a_row_error_marks_the_job_failed(monkeypatch, spooled):
    _patch_service(
        monkeypatch,
        _FakeService(result={"findings_imported": 1, "findings_errors": 2}),
    )
    job = IngestionJob("export.csv", "csv", "finding")

    run_job(job, spooled)

    assert job.status == FAILED
    assert "2 finding errors" in job.message


def test_run_job_forwards_the_declared_data_type(monkeypatch, spooled):
    service = _FakeService(result={"cases_imported": 1})
    _patch_service(monkeypatch, service)
    job = IngestionJob("cases.jsonl", "jsonl", "case")

    run_job(job, spooled)

    assert service.calls == [(spooled, "jsonl", "case")]


# --- progress -------------------------------------------------------------


def test_progress_is_readable_while_the_job_runs(monkeypatch, spooled):
    job = IngestionJob("export.parquet", "parquet", "finding")
    seen = []

    def mid_run(stats):
        stats["findings_total"] = 10
        stats["findings_imported"] = 4
        seen.append(job.snapshot())

    _patch_service(monkeypatch, _FakeService(result={}, on_ingest=mid_run))
    run_job(job, spooled)

    assert seen[0]["status"] == RUNNING
    assert seen[0]["processed"] == 4
    assert seen[0]["total"] == 10
    assert seen[0]["determinate"] is True


def test_row_counting_formats_are_not_reported_as_determinate():
    assert IngestionJob("a.csv", "csv", "finding").snapshot()["determinate"] is False
    assert (
        IngestionJob("a.jsonl", "jsonl", "finding").snapshot()["determinate"] is False
    )
    assert IngestionJob("a.json", "json", "finding").snapshot()["determinate"] is True


def test_processed_counts_skips_and_errors_not_just_imports(monkeypatch, spooled):
    _patch_service(
        monkeypatch,
        _FakeService(
            result={
                "findings_total": 6,
                "findings_imported": 2,
                "findings_skipped": 3,
                "findings_errors": 1,
            }
        ),
    )
    job = IngestionJob("export.parquet", "parquet", "finding")

    run_job(job, spooled)

    assert job.snapshot()["processed"] == 6


# --- registry -------------------------------------------------------------


def test_registry_admits_only_one_running_job():
    registry = IngestionJobRegistry()
    first = IngestionJob("a.parquet", "parquet", "finding")
    registry.start(first)

    with pytest.raises(IngestionJobConflict) as excinfo:
        registry.start(IngestionJob("b.parquet", "parquet", "finding"))

    assert excinfo.value.active is first


def test_a_finished_job_frees_the_slot():
    registry = IngestionJobRegistry()
    first = IngestionJob("a.parquet", "parquet", "finding")
    registry.start(first)
    first.finish("Imported 1 findings")

    registry.start(IngestionJob("b.parquet", "parquet", "finding"))

    assert registry.active().filename == "b.parquet"


def test_a_failed_job_frees_the_slot():
    registry = IngestionJobRegistry()
    first = IngestionJob("a.parquet", "parquet", "finding")
    registry.start(first)
    first.fail("boom")

    registry.start(IngestionJob("b.parquet", "parquet", "finding"))

    assert registry.active().filename == "b.parquet"


def test_recent_lists_newest_first():
    registry = IngestionJobRegistry()
    for name in ("a", "b", "c"):
        job = IngestionJob(f"{name}.parquet", "parquet", "finding")
        registry.start(job)
        job.finish("done")

    assert [j.filename for j in registry.recent()] == [
        "c.parquet",
        "b.parquet",
        "a.parquet",
    ]


def test_registry_is_bounded():
    registry = IngestionJobRegistry(max_tracked=2)
    for name in ("a", "b", "c"):
        job = IngestionJob(f"{name}.parquet", "parquet", "finding")
        registry.start(job)
        job.finish("done")

    assert [j.filename for j in registry.recent()] == ["c.parquet", "b.parquet"]


def test_get_returns_none_for_an_unknown_job():
    assert IngestionJobRegistry().get("ing-nope") is None
