from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_TRACKED_JOBS = 20

DETERMINATE_FORMATS = frozenset({"parquet", "json"})  # row count known upfront

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

_PROGRESS_KEYS = tuple(
    f"{kind}_{outcome}"
    for kind in ("findings", "cases")
    for outcome in ("imported", "skipped", "errors")
)


class IngestionJobConflict(RuntimeError):
    def __init__(self, active: "IngestionJob"):
        super().__init__(f"Ingestion job {active.job_id} is already running")
        self.active = active


class IngestionJob:
    def __init__(self, filename: str, fmt: str, data_type: str):
        self.job_id = f"ing-{uuid.uuid4().hex[:12]}"
        self.filename = filename
        self.format = fmt
        self.data_type = data_type
        self.status = RUNNING
        self.created_at = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self.message = ""
        self.error: Optional[str] = None
        self._stats: Dict[str, int] = {}

    def track(self, stats: Dict[str, int]) -> None:
        """Adopt the service's stats dict as this job's live progress source."""
        self._stats = stats

    def finish(self, message: str) -> None:
        self.status = SUCCEEDED
        self.message = message
        self.finished_at = datetime.now(timezone.utc)

    def fail(self, error: str) -> None:
        self.status = FAILED
        self.error = error
        self.message = error
        self.finished_at = datetime.now(timezone.utc)

    def snapshot(self) -> Dict[str, Any]:
        """Readable while the job runs; counts are ints written by one thread."""
        stats = dict(self._stats)
        processed = sum(stats.get(key, 0) for key in _PROGRESS_KEYS)
        total = stats.get("findings_total", 0) + stats.get("cases_total", 0)
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "format": self.format,
            "data_type": self.data_type,
            "status": self.status,
            "determinate": self.format in DETERMINATE_FORMATS,
            "processed": processed,
            "total": total,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "message": self.message,
            "error": self.error,
            "stats": stats,
        }


class IngestionJobRegistry:
    """Bounded, in-memory job store admitting one running ingest at a time."""

    def __init__(self, max_tracked: int = MAX_TRACKED_JOBS):
        self._lock = threading.Lock()
        self._jobs: Dict[str, IngestionJob] = {}
        self._max_tracked = max_tracked

    def start(self, job: IngestionJob) -> None:
        """Admit a job, or raise IngestionJobConflict if one is still running."""
        with self._lock:
            active = self._active_locked()
            if active is not None:
                raise IngestionJobConflict(active)
            self._jobs[job.job_id] = job
            self._prune_locked()

    def active(self) -> Optional[IngestionJob]:
        with self._lock:
            return self._active_locked()

    def get(self, job_id: str) -> Optional[IngestionJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> List[IngestionJob]:
        """Tracked jobs, newest first."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def _active_locked(self) -> Optional[IngestionJob]:
        for job in reversed(self._jobs.values()):
            if job.status == RUNNING:
                return job
        return None

    def _prune_locked(self) -> None:
        while len(self._jobs) > self._max_tracked:
            oldest = next(iter(self._jobs))
            if self._jobs[oldest].status == RUNNING:
                break
            del self._jobs[oldest]


_registry = IngestionJobRegistry()


def get_job_registry() -> IngestionJobRegistry:
    return _registry


def summarize_stats(stats: Dict[str, int]) -> tuple:
    """Reduce ingestion stats to a (success, human message) pair."""
    imported = stats.get("findings_imported", 0) + stats.get("cases_imported", 0)
    skipped = stats.get("findings_skipped", 0) + stats.get("cases_skipped", 0)
    errors = stats.get("findings_errors", 0) + stats.get("cases_errors", 0)
    success = errors == 0 and (imported > 0 or skipped > 0)

    messages = []
    if stats.get("findings_imported", 0) > 0:
        messages.append(f"Imported {stats['findings_imported']} findings")
    if stats.get("findings_skipped", 0) > 0:
        messages.append(f"Skipped {stats['findings_skipped']} duplicate findings")
    if stats.get("cases_imported", 0) > 0:
        messages.append(f"Imported {stats['cases_imported']} cases")
    if stats.get("cases_skipped", 0) > 0:
        messages.append(f"Skipped {stats['cases_skipped']} duplicate cases")
    if stats.get("findings_errors", 0) > 0:
        messages.append(f"{stats['findings_errors']} finding errors")
    if stats.get("cases_errors", 0) > 0:
        messages.append(f"{stats['cases_errors']} case errors")

    return success, ". ".join(messages) if messages else "No data imported"


def run_job(job: IngestionJob, source_path: Path) -> None:
    """Ingest source_path on a worker thread, then delete it. Never raises."""
    from core.ingestion.ingestion_service import IngestionService

    try:
        service = IngestionService()
        job.track(service.stats)
        stats = service._ingest_file_by_format(
            source_path, job.format, data_type=job.data_type
        )
        success, message = summarize_stats(stats or {})
        if success:
            job.finish(message)
        else:
            job.fail(message)
    except Exception as e:
        logger.error(f"Ingestion job {job.job_id} failed: {e}", exc_info=True)
        job.fail(f"Ingestion failed: {e}")
    finally:
        source_path.unlink(missing_ok=True)
