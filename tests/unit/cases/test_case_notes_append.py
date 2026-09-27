"""PATCH /api/cases/{id} must append a notes entry, not assign a string.

``Case.notes`` is a ``JSONBList`` (#709). Writing a plain str raises
``ValueError`` inside ``update_case``, which the endpoint used to surface
as 404 and drop the rest of the payload. The router now does the same
read-modify-write as ``add_case_activity``: load, append
``{timestamp, content}``, write the full list.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

# The endpoint takes a session and a principal since #733, because a status edit
# to `closed` records who closed it. Neither is exercised by a notes append.
SESSION = MagicMock()
ANALYST = SimpleNamespace(username="nestor")


def _patch_data_service(monkeypatch, *, case, update=None):
    from core.api.v1 import cases_router as cases

    captured = {}

    def _update(case_id, **updates):
        if isinstance(updates.get("notes"), str):
            raise ValueError(
                "Attribute 'notes' does not accept objects of type <class 'str'>"
            )
        captured["case_id"] = case_id
        captured["updates"] = updates
        return True if update is None else update(case_id, **updates)

    monkeypatch.setattr(cases.data_service, "get_case", lambda case_id: case)
    monkeypatch.setattr(cases.data_service, "update_case", _update)
    return cases, captured


@pytest.mark.asyncio
async def test_patch_appends_a_note_entry(monkeypatch):
    from core.api.v1.cases_router import CaseUpdate

    existing = {
        "case_id": "c1",
        "notes": [{"timestamp": "2026-01-01T00:00:00Z", "content": "prior"}],
    }
    cases, captured = _patch_data_service(monkeypatch, case=existing)

    result = await cases.update_case(
        "c1", CaseUpdate(notes="analyst comment"), SESSION, ANALYST
    )

    assert result == {"success": True}
    notes = captured["updates"]["notes"]
    assert notes[0]["content"] == "prior"
    assert notes[1]["content"] == "analyst comment"
    assert notes[1]["timestamp"].endswith("Z")
    assert set(notes[1]) == {"timestamp", "content"}


@pytest.mark.asyncio
async def test_patch_notes_starts_a_list_when_case_has_none(monkeypatch):
    from core.api.v1.cases_router import CaseUpdate

    cases, captured = _patch_data_service(
        monkeypatch, case={"case_id": "c1", "notes": None}
    )

    await cases.update_case("c1", CaseUpdate(notes="first"), SESSION, ANALYST)

    notes = captured["updates"]["notes"]
    assert len(notes) == 1
    assert notes[0]["content"] == "first"


@pytest.mark.asyncio
async def test_patch_keeps_other_fields_when_appending_notes(monkeypatch):
    from core.api.v1.cases_router import CaseUpdate

    cases, captured = _patch_data_service(
        monkeypatch, case={"case_id": "c1", "notes": []}
    )

    await cases.update_case(
        "c1", CaseUpdate(title="retitled", notes="wrapped"), SESSION, ANALYST
    )

    updates = captured["updates"]
    assert updates["title"] == "retitled"
    assert isinstance(updates["notes"], list)
    assert updates["notes"][0]["content"] == "wrapped"


@pytest.mark.asyncio
async def test_patch_notes_404_when_case_missing(monkeypatch):
    from core.api.v1 import cases_router as cases
    from core.api.v1.cases_router import CaseUpdate

    monkeypatch.setattr(cases.data_service, "get_case", lambda case_id: None)
    monkeypatch.setattr(cases.data_service, "update_case", MagicMock())

    with pytest.raises(HTTPException) as exc:
        await cases.update_case("missing", CaseUpdate(notes="nope"), SESSION, ANALYST)

    assert exc.value.status_code == 404
    cases.data_service.update_case.assert_not_called()
