"""The daemon, the runs it launches and integration pushes work on real records.

Demo mode swaps what the console reads. With it on, a triage written into the
demo set is lost, a probe the demo set cannot find is re-queued every sweep, a
vStrike push is acknowledged and dropped, and a run on a real case cannot find
it and files its links and handoff case into the demo set.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.agents import tool_registry
from core.integrations.mcp.surface import acting_as
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
    REPO / "core" / "workflows" / "run_bridge_router.py",
    REPO / "core" / "workflows" / "workflows_service.py",
]
# Agent tools, reached by runs and by people in chat alike: each one follows demo
# mode only with a person behind the call (follow_demo=current_caller() is not None).
CALLER_AWARE = [
    REPO / "core" / "agents" / "tool_registry.py",
    REPO / "core" / "detections" / "tools.py",
    REPO / "tools" / "mcp" / "vigil.py",
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


def _follow_demo_arguments(path: Path):
    """``(line, the follow_demo argument or None)`` per DatabaseDataService built."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == (
            "DatabaseDataService"
        ):
            given = [k.value for k in node.keywords if k.arg == "follow_demo"]
            yield node.lineno, given[0] if given else None


def test_every_daemon_and_push_data_service_ignores_demo_mode():
    unpinned = [
        f"{path.relative_to(REPO)}:{line}"
        for path in PINNED
        for line, arg in _follow_demo_arguments(path)
        if not (isinstance(arg, ast.Constant) and arg.value is False)
    ]
    assert not unpinned


def test_every_agent_tool_data_service_follows_the_caller():
    """Not a constant: ``current_caller() is not None``, or a name bound to it."""
    found, wrong = 0, []
    for path in CALLER_AWARE:
        for line, arg in _follow_demo_arguments(path):
            found += 1
            text = ast.unparse(arg) if arg is not None else ""
            if text not in ("current_caller() is not None", "follow_demo"):
                wrong.append(f"{path.relative_to(REPO)}:{line} {text!r}")
    assert found >= len(CALLER_AWARE)
    assert not wrong


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


@pytest.fixture
def postgres(monkeypatch):
    """Postgres as a mock that any newly built DatabaseDataService connects to."""
    db = MagicMock()
    healthy = MagicMock()
    healthy.health_check.return_value = True
    prefix = "core.storage.database_data_service"
    monkeypatch.setattr(f"{prefix}.init_database", lambda **_: None)
    monkeypatch.setattr(f"{prefix}.get_db_manager", lambda: healthy)
    monkeypatch.setattr(f"{prefix}.DatabaseService", lambda: db)
    monkeypatch.setattr(
        f"{prefix}.FindingSchema.dump", lambda row, **_: {"finding_id": "f-real"}
    )
    return db


def test_a_run_finds_and_links_a_real_finding(demo_on, postgres):
    from core.workflows.run_bridge_router import _link_findings

    postgres.get_finding.return_value = object()

    _link_findings("case-real", "run-1", ["f-real"])

    postgres.get_finding.assert_called_once_with("f-real")


def test_agent_tools_follow_demo_mode_only_with_a_person_behind_them(demo_on):
    from tools.mcp import vigil as vigil_mcp

    assert tool_registry._data().is_demo_mode() is False
    assert vigil_mcp.get_data_service().is_demo_mode() is False
    with acting_as("analyst"):
        assert tool_registry._data().is_demo_mode() is True
        assert vigil_mcp.get_data_service().is_demo_mode() is True


@pytest.mark.asyncio
async def test_reconstruct_run_reads_postgres_for_a_run(demo_on, postgres):
    from core.detections.tools import SecurityDetectionsTools

    tools = object.__new__(SecurityDetectionsTools)  # no rule corpus needed
    postgres.get_findings.return_value = []

    await tools.reconstruct_run(steps=[])
    assert postgres.get_findings.call_count == 1

    with acting_as("analyst"):  # a person in chat sees the demo set instead
        await tools.reconstruct_run(steps=[])
    assert postgres.get_findings.call_count == 1
