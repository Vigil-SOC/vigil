# A hunt writes no phase rows, so the run detail the console expands would say
# "no additional detail" for every hunt. It carries the agent layer's standing instead.

from __future__ import annotations

import pytest

from core.workflows import catalog
from core.workflows import workflows_router as router
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

STANDING = {"status": "active", "iteration": 2, "hypotheses": [], "evidence_count": 0}


def _runs_for(workflow_id: str):
    class _Runs:
        def get_run(self, run_id):
            return {"run_id": run_id, "workflow_id": workflow_id}

        def list_phases(self, _run_id):
            return []

    return _Runs()


async def _reads(_run_id, _evidence_limit=None):
    return STANDING


@pytest.mark.asyncio
async def test_a_hunt_run_carries_the_standing_of_its_hypotheses(monkeypatch):
    monkeypatch.setattr(router, "read_projection", _reads)

    detail = await router.get_workflow_run(
        "run-1", _runs_for("threat-hunt"), WorkflowsService()
    )

    assert detail["hunt"] == STANDING
    assert "projection" not in detail


# A non-hunt fold is the same read, under `projection`. `hunt` stays hunt-like.
@pytest.mark.asyncio
async def test_a_non_hunt_run_carries_the_fold_as_projection(monkeypatch):
    asked = []

    async def _record(run_id, _evidence_limit=None):
        asked.append(run_id)
        return STANDING

    monkeypatch.setattr(router, "read_projection", _record)

    detail = await router.get_workflow_run(
        "run-2", _runs_for("incident-response"), WorkflowsService()
    )

    assert asked == ["run-2"]
    assert detail["projection"] == STANDING
    assert "hunt" not in detail


def test_the_threat_hunt_definition_is_the_one_that_reads_as_a_hunt():
    workflows = WorkflowsService()
    assert catalog.is_hunt(workflows, "threat-hunt")
    assert not catalog.is_hunt(workflows, "incident-response")
    assert not catalog.is_hunt(workflows, None)


# The tier is a display label stamped on the way out, not stored on the row.
@pytest.mark.asyncio
async def test_a_hunt_run_stamps_each_evidence_row_with_its_source_tier(monkeypatch):
    fold = {
        "evidence": [
            {"evidence_id": "e1", "source_system": "crowdstrike", "is_gap": False},
            {"evidence_id": "e2", "source_system": "", "is_gap": True},
        ]
    }

    async def _reads(_run_id, _evidence_limit=None):
        return fold

    monkeypatch.setattr(router, "read_projection", _reads)

    detail = await router.get_workflow_run(
        "run-3", _runs_for("threat-hunt"), WorkflowsService()
    )

    live, gap = detail["hunt"]["evidence"]
    assert live["source_tier"] in {"telemetry", "feed", "not_evidence"}
    assert "source_tier" not in gap


@pytest.mark.asyncio
async def test_a_hunt_run_with_no_fold_still_reads(monkeypatch):
    async def _reads(_run_id, _evidence_limit=None):
        return None

    monkeypatch.setattr(router, "read_projection", _reads)

    detail = await router.get_workflow_run(
        "run-4", _runs_for("threat-hunt"), WorkflowsService()
    )

    assert detail["hunt"] is None


# #2287: a row names the system its rows came from, and is graded by it, not by the
# hunt's domain word. The alerts it cites win; the integration that answered stands in.
@pytest.mark.asyncio
async def test_a_hunt_row_names_the_system_its_rows_came_from(monkeypatch):
    fold = {
        "evidence": [
            {
                "evidence_id": "e1",
                "source_system": "process_lineage",
                "finding_ids": ["dl-003", "dl-005", "gone"],
                "served_by": None,
                "is_gap": False,
            },
            {
                "evidence_id": "e2",
                "source_system": "proxy",
                "finding_ids": ["dl-004", "dl-001"],
                "is_gap": False,
            },
            {
                "evidence_id": "e3",
                "source_system": "net_flow",
                "finding_ids": [],
                "served_by": "splunk-selfhosted",
                "is_gap": False,
            },
            {"evidence_id": "e4", "source_system": "dns", "is_gap": False},
            {"evidence_id": "e5", "source_system": "deployment", "is_gap": True},
        ]
    }
    asked = []

    async def _reads(_run_id, _evidence_limit=None):
        return fold

    def _sources(ids):
        asked.append(ids)
        known = {
            "dl-001": "proofpoint",
            "dl-003": "crowdstrike",
            "dl-004": "zscaler",
            "dl-005": "crowdstrike",
        }
        return {one: known[one] for one in ids if one in known}

    monkeypatch.setattr(router, "read_projection", _reads)
    monkeypatch.setattr(router, "_finding_sources", _sources)

    detail = await router.get_workflow_run(
        "run-5", _runs_for("threat-hunt"), WorkflowsService()
    )

    process, proxy, flow, dns, gap = detail["hunt"]["evidence"]
    # One read for the whole page, not one per row.
    assert asked == [["dl-001", "dl-003", "dl-004", "dl-005", "gone"]]
    assert process["origins"] == ["crowdstrike"]
    assert process["source_tier"] == "telemetry"
    assert proxy["origins"] == ["zscaler", "proofpoint"]
    assert proxy["source_tier"] == "telemetry"
    assert flow["origins"] == ["splunk-selfhosted"]
    assert flow["source_tier"] == "telemetry"
    # No system known: the domain is all it can be graded by, and is a declared one.
    assert "origins" not in dns
    assert dns["source_tier"] == "telemetry"
    assert "origins" not in gap and "source_tier" not in gap


# A system the tier map grades a feed pulls a row resting on it down with it.
@pytest.mark.asyncio
async def test_a_row_resting_on_several_systems_takes_the_weakest_tier(monkeypatch):
    fold = {
        "evidence": [
            {
                "evidence_id": "e1",
                "source_system": "proxy",
                "finding_ids": ["a", "b"],
                "is_gap": False,
            }
        ]
    }

    async def _reads(_run_id, _evidence_limit=None):
        return fold

    monkeypatch.setattr(router, "read_projection", _reads)
    monkeypatch.setattr(
        router,
        "_finding_sources",
        lambda ids: {"a": "crowdstrike", "b": "virustotal"},
    )

    detail = await router.get_workflow_run(
        "run-6", _runs_for("threat-hunt"), WorkflowsService()
    )

    assert detail["hunt"]["evidence"][0]["source_tier"] == "feed"


# #2458: a row the hunt read off Vigil itself names the part of Vigil that answered,
# and alert ids a model wrote into one string are each looked up.
@pytest.mark.asyncio
async def test_a_hunt_row_read_off_vigil_names_the_part_that_answered(monkeypatch):
    fold = {
        "calls": [
            {"tool": "search_findings", "iteration": 15},
            {"tool": "get_finding", "iteration": 15},
            {"tool": "search_findings", "iteration": 10},
            {"tool": "recall_entity", "iteration": 10},
            {"tool": "recall_entity", "iteration": 12},
            {"tool": "search_findings", "iteration": 14},
            {"tool": "splunk_execute", "iteration": 14},
        ],
        "evidence": [
            {
                "evidence_id": "e1",
                "iteration": 15,
                "source_system": "dns",
                "provenance": "worker",
                "is_gap": False,
            },
            {
                "evidence_id": "e2",
                "iteration": 10,
                "source_system": "endpoint",
                "provenance": "worker",
                "is_gap": False,
            },
            {
                "evidence_id": "e3",
                "iteration": 12,
                "source_system": "undeclared",
                "provenance": "worker",
                "is_gap": False,
            },
            # Another tool in its turn: which one answered is not known, so it is not said.
            {
                "evidence_id": "e4",
                "iteration": 14,
                "source_system": "proxy",
                "provenance": "worker",
                "is_gap": False,
            },
            # The critic's rebuttal is no read of anything.
            {
                "evidence_id": "e5",
                "iteration": 15,
                "source_system": "critic",
                "provenance": "null_check",
                "is_gap": False,
            },
            {
                "evidence_id": "e6",
                "iteration": 20,
                "source_system": "undeclared",
                "provenance": "worker",
                "finding_ids": ["dl-007;dl-001"],
                "is_gap": False,
            },
        ],
    }

    async def _reads(_run_id, _evidence_limit=None):
        return fold

    monkeypatch.setattr(router, "read_projection", _reads)
    monkeypatch.setattr(
        router,
        "_finding_sources",
        lambda ids: {
            one: {"dl-001": "proofpoint", "dl-007": "infoblox"}[one] for one in ids
        },
    )

    detail = await router.get_workflow_run(
        "run-7", _runs_for("threat-hunt"), WorkflowsService()
    )

    alerts, mixed, memory, other, critic, joined = detail["hunt"]["evidence"]
    assert alerts["origins"] == ["vigil_alerts"]
    assert mixed["origins"] == ["vigil"]
    assert memory["origins"] == ["vigil_memory"]
    assert "origins" not in other
    assert "origins" not in critic
    assert joined["origins"] == ["infoblox", "proofpoint"]
    # Graded as before: by the domain it was recorded under.
    assert alerts["source_tier"] == "telemetry"
