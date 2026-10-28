"""PATCH /api/cases/{id} must apply `assignee` (#2056); blank unassigns.

The value must name one active person, stored as their username (#3155).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from core.cases.assignee import AssigneeRefused
from tests.unit.cases.people import people

pytestmark = pytest.mark.unit

ANALYST = SimpleNamespace(username="nestor", user_id="nestor")


def _patch(monkeypatch, stored=None):
    from core.api.v1 import cases_router as cases

    captured = {}

    def _update(case_id, **updates):
        captured["updates"] = updates
        return True

    monkeypatch.setattr(
        cases.data_service,
        "get_case",
        lambda case_id: {"case_id": case_id, "assignee": stored},
    )
    monkeypatch.setattr(cases.data_service, "update_case", _update)
    # The caller holds cases.assign; the role gate is not under test.
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission", lambda *_a, **_k: True
    )
    return cases, captured


def _patch_case(cases, data):
    from core.api.v1.cases_router import CaseUpdate

    return cases.update_case(
        "c1", CaseUpdate(**data), people(), BackgroundTasks(), ANALYST
    )


@pytest.mark.parametrize("sent", [" alice ", "ALICE", "alice@example.com"])
def test_assignee_is_stored_as_the_username(monkeypatch, sent):
    cases, captured = _patch(monkeypatch)
    assert _patch_case(cases, {"assignee": sent}) == {"success": True}
    assert captured["updates"] == {"assignee": "alice"}


@pytest.mark.parametrize(
    "sent", ["no-such-person", "Alice Example", "bob", "medic-bot", "carol"]
)
def test_an_assignee_that_is_not_one_active_person_is_refused(monkeypatch, sent):
    cases, captured = _patch(monkeypatch)
    with pytest.raises(AssigneeRefused):
        _patch_case(cases, {"assignee": sent})
    assert "updates" not in captured


def test_an_unchanged_stored_assignee_is_kept(monkeypatch):
    # The edit form echoes the stored value back with every save.
    cases, captured = _patch(monkeypatch, stored="Alice Example")
    _patch_case(cases, {"assignee": "Alice Example", "title": "retitled"})
    assert captured["updates"]["assignee"] == "Alice Example"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_assignee_unassigns(monkeypatch, blank):
    cases, captured = _patch(monkeypatch)
    _patch_case(cases, {"assignee": blank})
    assert captured["updates"] == {"assignee": None}


def test_assignee_left_out_when_not_sent(monkeypatch):
    cases, captured = _patch(monkeypatch)
    _patch_case(cases, {"title": "retitled"})
    assert "assignee" not in captured["updates"]
