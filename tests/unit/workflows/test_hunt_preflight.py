# The start-a-hunt modal's preflight is about executing a run, so it has its own
# console route and the frozen catalog read carries the same keys for every kind.

from __future__ import annotations

import pytest

from core.workflows import catalog, hunt_preflight
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

PREFLIGHT_KEYS = {"capabilities", "pricing", "budgets"}


def test_a_hunt_is_told_its_ceiling_capabilities_and_rate():
    report = hunt_preflight.preflight(WorkflowsService(), None, "threat-hunt")

    assert set(report) == PREFLIGHT_KEYS
    assert set(report["budgets"]) == {"max_iterations", "max_cost_usd"}
    assert set(report["capabilities"]) >= {"bound", "unbound"}


def test_a_non_hunt_has_nothing_to_warn_about_and_an_unknown_id_is_none():
    workflows = WorkflowsService()

    assert hunt_preflight.preflight(workflows, None, "incident-response") == {}
    assert hunt_preflight.preflight(workflows, None, "no-such-workflow") is None


def test_the_catalog_read_of_a_hunt_carries_no_preflight():
    detail = catalog.detail(WorkflowsService(), "threat-hunt")

    assert detail["hunt_like"] is True
    assert not PREFLIGHT_KEYS & set(detail)
