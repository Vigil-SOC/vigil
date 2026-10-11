"""GET /cases/{id}/sla answers null for a case with no SLA, not a 404 the browser logs."""

from __future__ import annotations

import asyncio

import pytest

from services.api.routers import cases

pytestmark = pytest.mark.unit


def test_case_without_an_sla_gets_null(monkeypatch):
    monkeypatch.setattr(
        cases.CaseSLAService, "get_sla_status", lambda self, case_id: None
    )

    assert asyncio.run(cases.get_case_sla("case-1")) is None


def test_case_with_an_sla_gets_its_status(monkeypatch):
    status = {"case_id": "case-1", "health_status": "healthy"}
    monkeypatch.setattr(
        cases.CaseSLAService, "get_sla_status", lambda self, case_id: status
    )

    assert asyncio.run(cases.get_case_sla("case-1")) == status

