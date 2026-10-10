"""PATCH /api/cases/{id} must apply `assignee` (#2056); blank unassigns."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

pytestmark = pytest.mark.unit

SESSION = MagicMock()
ANALYST = SimpleNamespace(username="nestor")


def _patch(monkeypatch):
    from core.api.v1 import cases_router as cases

    captured = {}

    def _update(case_id, **updates):
        captured["updates"] = updates
        return True

    monkeypatch.setattr(
        cases.data_service, "get_case", lambda case_id: {"case_id": case_id}
    )
    monkeypatch.setattr(cases.data_service, "update_case", _update)
    return cases, captured


def _patch_case(cases, data):
    from core.api.v1.cases_router import CaseUpdate

    return cases.update_case(
        "c1", CaseUpdate(**data), SESSION, BackgroundTasks(), ANALYST
    )


def test_assignee_is_applied(monkeypatch):
    cases, captured = _patch(monkeypatch)
    assert _patch_case(cases, {"assignee": " analyst "}) == {"success": True}
    assert captured["updates"] == {"assignee": "analyst"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_assignee_unassigns(monkeypatch, blank):
    cases, captured = _patch(monkeypatch)
    _patch_case(cases, {"assignee": blank})
    assert captured["updates"] == {"assignee": None}


def test_assignee_left_out_when_not_sent(monkeypatch):
    cases, captured = _patch(monkeypatch)
    _patch_case(cases, {"title": "retitled"})
    assert "assignee" not in captured["updates"]
