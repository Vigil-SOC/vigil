# Coverage check (#903): a report in, one of three answers out, and no hunt
# started. parse_report and list_prior_hunts are the siblings' and are patched.

from __future__ import annotations

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS
from core.memory import hunt_coverage
from core.memory.hunt_coverage import check_coverage
from core.workflows.workflows_router import WorkflowExecuteRequest

pytestmark = pytest.mark.unit

IP = "ip:203.0.113.7"
DOMAIN = "domain:evil.example"
T_ID = "T1566"

RUNNING = {
    "run_id": "wfr-live",
    "status": "running",
    "started_at": "2026-09-01T00:00:00Z",
    "hypothesis": "Phishing via T1566 from 203.0.113.7",
    "matched_keys": [IP],
    "matched_techniques": [T_ID],
}

VERDICT = {
    "investigation_id": "inv-1",
    "hypothesis_id": "h-1",
    "statement": "203.0.113.7 is C2",
    "outcome": "confirmed",
    "concluded_at": "2026-08-01T00:00:00Z",
    "origin_run_id": "wfr-done",
    "matched_keys": [IP],
    "matched_techniques": [],
}


@pytest.fixture
def no_hunt_started(monkeypatch):
    """Fail the test if anything reaches for the execute or directive path."""

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("coverage check must not start or extend a hunt")

    monkeypatch.setattr(
        "core.workflows.workflows_service.WorkflowsService.execute_workflow",
        _forbidden,
    )
    monkeypatch.setattr("core.agents.directives.enqueue_directive", _forbidden)


def _install(monkeypatch, *, concluded=(), in_flight=(), parsed=None):
    asked = {}

    def _list(entity_keys, techniques=()):
        asked["keys"], asked["techniques"] = list(entity_keys), list(techniques)
        return {
            "keys": sorted(set(asked["keys"])),
            "techniques": sorted(set(asked["techniques"])),
            "concluded": list(concluded),
            "in_flight": list(in_flight),
        }

    monkeypatch.setattr(hunt_coverage, "list_prior_hunts", _list)
    monkeypatch.setattr(
        hunt_coverage,
        "parse_report",
        lambda text: parsed or {"entity_keys": [], "techniques": []},
    )
    return asked


def test_running_hunt_wins_and_points_at_extend(monkeypatch, no_hunt_started):
    _install(monkeypatch, in_flight=[RUNNING], concluded=[VERDICT])

    result = check_coverage(entity_keys=[IP, DOMAIN], techniques=[T_ID])

    assert result["status"] == "running"
    assert result["in_flight"] == [RUNNING]
    assert result["extend"]["path"] == "/api/agent-runs/{run_id}/directives"
    assert result["extend"]["kind"] == "extend"
    assert result["extend"]["run_ids"] == ["wfr-live"]
    assert result["matched_keys"] == [IP]
    assert result["unmatched_keys"] == [DOMAIN]
    assert result["matched_techniques"] == [T_ID]
    assert "proposal" not in result and "concluded" not in result


def test_distilled_verdict_is_concluded_with_a_proposal(monkeypatch, no_hunt_started):
    _install(monkeypatch, concluded=[VERDICT])

    result = check_coverage(entity_keys=[IP], techniques=[T_ID])

    assert result["status"] == "concluded"
    assert result["concluded"][0]["origin_run_id"] == "wfr-done"
    assert result["concluded"][0]["outcome"] == "confirmed"
    assert result["matched_keys"] == [IP]
    assert result["unmatched_techniques"] == [T_ID]
    assert result["proposal"]["approve_hypotheses"] is True


def test_unknown_keys_are_uncovered_with_an_executable_proposal(
    monkeypatch, no_hunt_started
):
    _install(monkeypatch)

    result = check_coverage(entity_keys=[IP], techniques=[T_ID])

    assert result["status"] == "uncovered"
    assert result["unmatched_keys"] == [IP]
    assert result["execute"]["path"] == "/api/workflows/threat-hunt/execute"
    proposal = result["proposal"]
    request = WorkflowExecuteRequest(**proposal)
    assert request.approve_hypotheses is True
    assert T_ID in request.hypothesis
    assert request.hypothesis_subjects == {request.hypothesis: [IP]}


def test_report_text_is_parsed_and_merged_with_given_keys(monkeypatch):
    asked = _install(monkeypatch, parsed={"entity_keys": [IP], "techniques": ["T1071"]})

    check_coverage(report="seen 203.0.113.7", entity_keys=[DOMAIN], techniques=[T_ID])

    assert asked["keys"] == [IP, DOMAIN]
    assert asked["techniques"] == ["T1071", T_ID]


def test_nothing_to_ask_about_is_refused(monkeypatch):
    _install(monkeypatch)

    with pytest.raises(ValueError, match="nothing to check"):
        check_coverage(report="   ")


@pytest.mark.asyncio
async def test_backend_tool_dispatches_the_check(monkeypatch, no_hunt_started):
    _install(monkeypatch)

    result, handled = await execute_backend_tool(
        "check_hunt_coverage", {"entity_keys": [IP], "techniques": [T_ID]}
    )

    assert handled is True
    assert result["status"] == "uncovered"


@pytest.mark.asyncio
async def test_backend_tool_reports_an_empty_ask_as_an_error(monkeypatch):
    _install(monkeypatch)

    result, handled = await execute_backend_tool("check_hunt_coverage", {})

    assert handled is True
    assert "error" in result


def test_schema_and_threat_intel_agent_name_the_tool():
    names = {tool["name"] for tool in ALL_TOOLS}
    assert "check_hunt_coverage" in names
    granted = [
        row["id"]
        for row in BUILTIN_AGENTS
        if "check_hunt_coverage" in row["recommended_tools"]
    ]
    assert granted == ["threat_intel"]
