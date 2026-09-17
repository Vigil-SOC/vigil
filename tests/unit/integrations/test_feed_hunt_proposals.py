# Hunt proposals from recent feed indicators (#905): the second caller of
# check_coverage. The classifier and the rows are patched; no hunt is started.

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS
from core.memory import hunt_coverage
from core.threat_intel import threat_feed_service as feed
from core.workflows import workflows_router as router
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

SEEN = datetime(2026, 9, 16, 12, 0, 0)


def _row(indicator_type, value, source="cloudforce_one"):
    return {
        "indicator_type": indicator_type,
        "indicator_value": value,
        "source": source,
        "last_seen": SEEN,
    }


@pytest.fixture
def no_hunt_started(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("feed proposals must not start a hunt")

    monkeypatch.setattr(WorkflowsService, "execute_workflow", _forbidden)
    monkeypatch.setattr("core.agents.directives.enqueue_directive", _forbidden)


def _install(monkeypatch, rows, covered=None):
    """Rows come from the patched read; each key's status from ``covered``."""
    asked = []
    covered = covered or {}
    monkeypatch.setattr(feed, "_recent_indicators", lambda limit: rows[:limit])

    def _check(report=None, entity_keys=(), techniques=()):
        keys = list(entity_keys)
        asked.append(keys)
        status = covered.get(keys[0], "uncovered")
        result = {"status": status, "keys": keys}
        if status != "running":
            result["proposal"] = hunt_coverage.build_proposal(keys, [])
            result["execute"] = {"method": "POST", "path": hunt_coverage.EXECUTE_PATH}
        return result

    monkeypatch.setattr(hunt_coverage, "check_coverage", _check)
    return asked


def test_uncovered_indicator_is_one_proposal(monkeypatch, no_hunt_started):
    asked = _install(monkeypatch, [_row("ip", "203.0.113.7")])

    result = feed.propose_hunts_from_recent_indicators()

    assert asked == [["ip:203.0.113.7"]]
    assert result["checked"] == 1 and result["read"] == 1
    [entry] = result["proposals"]
    assert entry["entity_key"] == "ip:203.0.113.7"
    assert entry["indicator"] == {
        "indicator_type": "ip",
        "indicator_value": "203.0.113.7",
        "source": "cloudforce_one",
        "last_seen": SEEN.isoformat(),
    }
    request = router.WorkflowExecuteRequest(**entry["proposal"])
    assert request.approve_hypotheses is True
    assert request.hypothesis_subjects == {request.hypothesis: ["ip:203.0.113.7"]}
    assert entry["execute"]["path"] == "/api/workflows/threat-hunt/execute"


def test_covered_indicators_are_counted_and_omitted(monkeypatch, no_hunt_started):
    rows = [
        _row("ip", "203.0.113.7"),
        _row("domain", "evil.example"),
        _row("hash_sha256", "ab" * 32),
    ]
    _install(
        monkeypatch,
        rows,
        covered={"ip:203.0.113.7": "concluded", "domain:evil.example": "running"},
    )

    result = feed.propose_hunts_from_recent_indicators()

    assert result["concluded"] == 1 and result["running"] == 1
    assert [p["entity_key"] for p in result["proposals"]] == [f"hash:{'ab' * 32}"]


def test_each_key_is_classified_on_its_own_and_once(monkeypatch):
    rows = [
        _row("ip", "203.0.113.7", source="feed_a"),
        _row("ip", "203.0.113.7", source="feed_b"),
        _row("ip", "203.0.113.8"),
    ]
    asked = _install(monkeypatch, rows)

    result = feed.propose_hunts_from_recent_indicators()

    assert asked == [["ip:203.0.113.7"], ["ip:203.0.113.8"]]
    assert result["checked"] == 2
    assert result["proposals"][0]["indicator"]["source"] == "feed_a"


def test_unmapped_type_is_skipped_not_minted(monkeypatch):
    asked = _install(monkeypatch, [_row("asn", "AS64496"), _row("ip", "203.0.113.7")])

    result = feed.propose_hunts_from_recent_indicators()

    assert asked == [["ip:203.0.113.7"]]
    assert result["skipped"] == 1
    assert len(result["proposals"]) == 1


def test_limit_is_capped_by_the_module_constant(monkeypatch):
    rows = [_row("ip", f"203.0.113.{i}") for i in range(3)]
    _install(monkeypatch, rows)

    assert feed.propose_hunts_from_recent_indicators(limit=2)["checked"] == 2
    assert feed.propose_hunts_from_recent_indicators(limit=10**6)["limit"] == (
        feed.RECENT_INDICATOR_LIMIT
    )


@pytest.mark.asyncio
async def test_backend_tool_dispatches_the_proposal(monkeypatch, no_hunt_started):
    _install(monkeypatch, [_row("ip", "203.0.113.7")])

    result, handled = await execute_backend_tool("propose_feed_hunts", {})

    assert handled is True
    assert len(result["proposals"]) == 1


def test_route_returns_the_same_shape(monkeypatch, no_hunt_started):
    _install(monkeypatch, [_row("ip", "203.0.113.7")])
    app = FastAPI()
    app.include_router(router.router, prefix=router.ROUTER_META.prefix)

    response = TestClient(app).get("/api/workflows/threat-hunt/feed-proposals")

    assert response.status_code == 200
    assert response.json()["proposals"][0]["entity_key"] == "ip:203.0.113.7"


def test_schema_and_threat_intel_agent_name_the_tool():
    assert "propose_feed_hunts" in {tool["name"] for tool in ALL_TOOLS}
    granted = [
        row["id"]
        for row in BUILTIN_AGENTS
        if "propose_feed_hunts" in row["recommended_tools"]
    ]
    assert granted == ["threat_intel"]
