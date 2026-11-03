"""Case distil concluded_at matches GREATEST(closed_at, updated_at) (#3662)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.memory import case_distil
from core.storage.models import Case, CaseClosureInfo

pytestmark = pytest.mark.unit


def test_accept_takes_later_of_closed_at_and_updated_at():
    closed_at = datetime(2026, 10, 28, 0, 16, 11, 317901)
    updated_at = datetime(2026, 10, 28, 0, 16, 11, 363542)

    case = MagicMock(spec=Case)
    case.case_id = "case-1"
    case.updated_at = updated_at

    closure = MagicMock(spec=CaseClosureInfo)
    closure.closed_at = closed_at

    session = MagicMock()
    session.get.return_value = closure

    closed = case_distil._accept(session, case)

    assert closed.concluded_at == updated_at.replace(tzinfo=timezone.utc)


def test_accept_uses_closed_at_when_newer_than_updated_at():
    updated_at = datetime(2026, 10, 28, 0, 16, 11, 100000)
    closed_at = datetime(2026, 10, 28, 0, 16, 11, 500000)

    case = MagicMock(spec=Case)
    case.case_id = "case-1"
    case.updated_at = updated_at

    closure = MagicMock(spec=CaseClosureInfo)
    closure.closed_at = closed_at

    session = MagicMock()
    session.get.return_value = closure

    closed = case_distil._accept(session, case)

    assert closed.concluded_at == closed_at.replace(tzinfo=timezone.utc)


def test_write_case_distil_loads_case_with_noload_findings(monkeypatch):
    monkeypatch.setattr(case_distil, "clear_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(case_distil, "clear_investigation", lambda *_a, **_k: None)
    monkeypatch.setattr(case_distil, "_withdraw", lambda *_a, **_k: {"withdrawn": 1})

    case = MagicMock(spec=Case)
    case.case_id = "case-1"
    case.status = "open"

    session = MagicMock()
    session.get.return_value = case

    result = case_distil.write_case_distil(session, "case-1")
    assert result == {"withdrawn": 1}

    options = session.get.call_args.kwargs.get("options")
    assert options and len(options) == 1
    assert any(entry.strategy == (("lazy", "noload"),) for entry in options[0].context)
