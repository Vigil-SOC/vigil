"""Finding.cases and Case.findings loader defaults (#3658).

Finding.cases is never serialized by FindingSchema and has no implicit reader,
so it must not default to lazy="selectin" (which SELECTs full case rows once
per finding and cascades back through Case.findings -> Finding.cases).
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect

from core.reporting.analytics_service import get_affected_entities
from core.storage.models import Case, Finding
from core.storage.service import DatabaseService

pytestmark = pytest.mark.unit


def test_finding_cases_relationship_is_lazy_select():
    mapper = sa_inspect(Finding)
    assert mapper.relationships["cases"].lazy == "select"


def test_get_case_without_findings_skips_mitre_prediction_rows(monkeypatch):
    session = MagicMock()
    case = MagicMock(spec=Case)
    case.findings = []
    session.get.return_value = case

    db_manager = MagicMock()

    @contextmanager
    def _scope():
        yield session

    db_manager.session_scope = _scope
    monkeypatch.setattr("core.storage.service.get_db_manager", lambda: db_manager)
    service = DatabaseService()

    result = service.get_case("case-1", include_findings=False)
    assert result is case
    options = session.get.call_args.kwargs.get("options")
    assert options and len(options) == 1
    assert any(entry.strategy == (("lazy", "noload"),) for entry in options[0].context)


@pytest.mark.asyncio
async def test_get_affected_entities_uses_noload_options():
    db = MagicMock()
    query = MagicMock()
    db.query.return_value = query
    query.options.return_value = query
    query.filter.return_value = query
    query.all.return_value = []

    now = datetime.now(timezone.utc)
    result = await get_affected_entities(db, now, now)

    assert result == []
    query.options.assert_called_once()
    opts = query.options.call_args.args
    assert len(opts) == 2
    assert all(
        any(entry.strategy == (("lazy", "noload"),) for entry in opt.context)
        for opt in opts
    )
