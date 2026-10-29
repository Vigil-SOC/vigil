import contextlib
import json
from types import SimpleNamespace

import pytest

import tools.mcp.vigil as vigil
from core.cases import case_records_service


@pytest.fixture
def call(monkeypatch):
    activities = []
    task = SimpleNamespace(task_id=5, case_id="case-1", title="Analyze")

    @contextlib.contextmanager
    def fake_session():
        yield object()

    monkeypatch.setattr(case_records_service, "update_task", lambda *a, **k: task)
    monkeypatch.setattr(vigil, "service_session", fake_session)
    monkeypatch.setattr(
        vigil.CaseTaskSchema, "dump", staticmethod(lambda t: {"task_id": t.task_id})
    )
    monkeypatch.setattr(vigil, "add_case_activity", lambda *a: activities.append(a))

    def _call(**kwargs):
        return json.loads(vigil.update_case_task(5, **kwargs)), activities

    return _call


def test_notes_without_status_are_recorded(call):
    result, activities = call(notes="ransomware variant confirmed")
    assert result["success"] is True
    assert activities == [
        (
            "case-1",
            "task_update",
            "Task 'Analyze' note added",
            {"task_id": 5, "notes": "ransomware variant confirmed"},
        )
    ]


def test_status_with_notes_records_status_change(call):
    _, activities = call(status="in_progress", notes="started")
    assert activities == [
        (
            "case-1",
            "task_update",
            "Task 'Analyze' status changed to in_progress",
            {"task_id": 5, "notes": "started"},
        )
    ]
