"""The daemon and integration pushes work on real records while demo mode is on.

Demo mode swaps what the console reads. A triage written into the demo set is
lost, a probe the demo set cannot find is re-queued every sweep, and a vStrike
push is acknowledged and dropped.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.storage.database_data_service import DatabaseDataService
from services.daemon.config import ProcessingConfig, SchedulerConfig
from services.daemon.probes import PROBES, inject_probes
from services.daemon.processor import FindingProcessor
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
# Everything here handles real records; the console's routers follow demo mode.
PINNED = [
    *sorted((REPO / "services" / "daemon").glob("*.py")),
    REPO / "services" / "api" / "routers" / "vstrike.py",
    REPO / "core" / "cases" / "case_automation_service.py",
]


@pytest.fixture
def demo_on():
    with patch("core.storage.database_data_service.is_demo_mode", return_value=True):
        yield


def _connect(service: DatabaseDataService) -> MagicMock:
    db = MagicMock()
    service._db_service = db
    service._db_connected = True
    return db


def test_every_daemon_and_push_data_service_ignores_demo_mode():
    unpinned = []
    for path in PINNED:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "DatabaseDataService"
            ):
                continue
            pins = [
                k
                for k in node.keywords
                if k.arg == "follow_demo"
                and isinstance(k.value, ast.Constant)
                and k.value.value is False
            ]
            if not pins:
                unpinned.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not unpinned


@pytest.mark.asyncio
async def test_a_triage_result_is_written_to_postgres(demo_on):
    processor = FindingProcessor(ProcessingConfig(auto_enrich_enabled=False))
    processor._init_services()
    db = _connect(processor._data_service)
    db.update_finding.return_value = True

    await processor._update_finding(
        {"finding_id": "f-real", "severity": "high", "status": "triaged"}
    )

    db.update_finding.assert_called_once_with(
        "f-real", severity="high", status="triaged"
    )
    assert processor.stats["errors"] == 0


@pytest.mark.asyncio
async def test_stored_probes_are_found_and_not_queued_again(demo_on):
    with patch("core.llm.harness.claude.ClaudeService", MagicMock()):
        scheduler = TaskScheduler(SchedulerConfig())
        scheduler._init_services()
    db = _connect(scheduler._data_service)
    db.get_finding.return_value = object()  # today's probes are already stored
    queue: asyncio.Queue = asyncio.Queue()

    with patch(
        "core.storage.database_data_service.FindingSchema.dump",
        return_value={"finding_id": "stored"},
    ):
        injected = await inject_probes(queue, scheduler._data_service)

    assert injected == 0
    assert queue.empty()
    assert db.get_finding.call_count == len(PROBES)
