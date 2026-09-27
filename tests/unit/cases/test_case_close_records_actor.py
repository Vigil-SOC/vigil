"""Every path that closes a Case records who closed it and why (#733).

This is the premise the whole of #733 rests on. Analysts do not close Cases
through ``POST /cases/{id}/close``; they PATCH ``status`` to ``closed`` from the
console, which recorded neither a category nor an actor. Episodic memory reads
Trust ``analyst`` off that record, so without it the highest-value thing the
system produces -- a person looked and said no -- reached memory as nothing.

Asserted at each writer rather than through the Distil, because what is wrong
when this breaks is the close, not the mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.cases.closure import ClosedByKind, ClosureCategory

pytestmark = pytest.mark.unit

ANALYST = SimpleNamespace(username="nestor")


class _Service:
    """Stands in for CaseWorkflowService, recording what the router asked of it."""

    calls: list = []

    def close_case(self, session, case_id, **kwargs):
        type(self).calls.append(("close", case_id, kwargs))
        return MagicMock()

    def reopen_case(self, session, case_id):
        type(self).calls.append(("reopen", case_id, {}))


@pytest.fixture
def service(monkeypatch):
    """Every close goes through the service, so that is what these assert on."""
    import core.cases.case_workflow_service as workflow

    _Service.calls = []
    monkeypatch.setattr(workflow, "CaseWorkflowService", _Service)
    return _Service


def _routes(monkeypatch, *, case, updated=True):
    from core.api.v1 import cases_router as cases

    captured = {}

    def _update(case_id, **updates):
        captured["updates"] = updates
        return updated

    monkeypatch.setattr(cases.data_service, "get_case", lambda case_id: case)
    monkeypatch.setattr(cases.data_service, "update_case", _update)
    return cases, captured


@pytest.mark.asyncio
async def test_a_status_edit_to_closed_records_the_principal(monkeypatch, service):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "investigating"})

    await cases.update_case("c1", CaseUpdate(status="closed"), MagicMock(), ANALYST)

    ((kind, case_id, kwargs),) = service.calls
    assert (kind, case_id) == ("close", "c1")
    # The name comes from the authenticated principal, never from the body:
    # a client-supplied one would let any caller claim an analyst concluded.
    assert kwargs["closed_by"] == "nestor"
    assert kwargs["closed_by_kind"] is ClosedByKind.ANALYST


@pytest.mark.asyncio
async def test_it_does_not_invent_a_determination(monkeypatch, service):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "open"})

    await cases.update_case("c1", CaseUpdate(status="closed"), MagicMock(), ANALYST)

    # `unspecified` is not one of the four determinations. It says the Case
    # closed and no reason was stated, which is what happened -- and the
    # service refuses to let it overwrite a determination already on record.
    assert service.calls[0][2]["closure_category"] is ClosureCategory.UNSPECIFIED


@pytest.mark.asyncio
async def test_it_closes_through_the_service_and_not_around_it(monkeypatch, service):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "open"})
    session = MagicMock()

    await cases.update_case("c1", CaseUpdate(status="closed"), session, ANALYST)

    # Writing the row here instead would skip the SLA resolution clock and the
    # IOC index, leaving a Case that is closed differently from every other one.
    assert service.calls[0][0] == "close"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_status_edit_to_something_else_records_nothing(monkeypatch, service):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "open"})

    await cases.update_case(
        "c1", CaseUpdate(status="investigating"), MagicMock(), ANALYST
    )

    assert service.calls == []


@pytest.mark.asyncio
async def test_re_saving_an_already_closed_case_is_an_edit_and_not_a_close(
    monkeypatch, service
):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "closed"})

    await cases.update_case(
        "c1", CaseUpdate(title="retitled", status="closed"), MagicMock(), ANALYST
    )

    # Closing again here would move the closure's date and re-derive its Verdict
    # for a change that concluded nothing new.
    assert service.calls == []


@pytest.mark.asyncio
async def test_a_failed_update_records_no_close(monkeypatch, service):
    from fastapi import HTTPException

    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(
        monkeypatch, case={"case_id": "c1", "status": "open"}, updated=False
    )

    with pytest.raises(HTTPException):
        await cases.update_case("c1", CaseUpdate(status="closed"), MagicMock(), ANALYST)

    assert service.calls == []


@pytest.mark.asyncio
async def test_reopening_retracts_the_determination(monkeypatch, service):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "closed"})

    await cases.update_case(
        "c1", CaseUpdate(status="investigating"), MagicMock(), ANALYST
    )

    assert service.calls == [("reopen", "c1", {})]


@pytest.mark.asyncio
async def test_an_edit_that_does_not_touch_status_retracts_nothing(
    monkeypatch, service
):
    from core.api.v1.cases_router import CaseUpdate

    cases, _ = _routes(monkeypatch, case={"case_id": "c1", "status": "closed"})

    await cases.update_case("c1", CaseUpdate(title="retitled"), MagicMock(), ANALYST)

    assert service.calls == []


@pytest.mark.asyncio
async def test_the_close_endpoint_takes_the_principal_and_not_the_body(
    monkeypatch, service
):
    from core.api.v1 import cases_router as cases
    from core.api.v1.cases_router import ClosureInfo

    monkeypatch.setattr(cases.CaseClosureInfoSchema, "dump", staticmethod(lambda r: {}))

    await cases.close_case(
        "c1",
        ClosureInfo(
            closure_category="false_positive",
            false_positive_reason="the scanner is ours",
        ),
        MagicMock(),
        ANALYST,
    )

    ((_, case_id, kwargs),) = service.calls
    assert case_id == "c1"
    assert kwargs["closed_by"] == "nestor"
    assert kwargs["closed_by_kind"] is ClosedByKind.ANALYST
    # Named in the rationale fallback and previously not an argument at all, so
    # the dedicated close path could never write the field it fell back to.
    assert kwargs["false_positive_reason"] == "the scanner is ours"


def test_the_close_request_has_no_closed_by_field():
    from core.api.v1.cases_router import ClosureInfo

    assert "closed_by" not in ClosureInfo.model_fields


def test_the_close_request_states_its_category_vocabulary():
    from pydantic import ValidationError

    from core.api.v1.cases_router import ClosureInfo

    # A typo used to close the Case and then reach memory as nothing: the
    # Distil has no outcome for an unknown category, so it wrote a marker and
    # no Verdict, and the Case never came back.
    with pytest.raises(ValidationError):
        ClosureInfo(closure_category="flase_positive")

    assert (
        ClosureInfo(closure_category="false_positive").closure_category
        is ClosureCategory.FALSE_POSITIVE
    )
