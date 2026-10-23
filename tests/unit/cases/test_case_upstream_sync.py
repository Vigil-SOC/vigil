import asyncio
import logging
from types import SimpleNamespace

import pytest

from core.api.v1 import cases_router as cases


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    state = SimpleNamespace(calls=calls, accepts=True, findings=[])

    class FakeElastic:
        async def update_upstream_alert_status(self, alert_id, status):
            calls.append((alert_id, status))
            return state.accepts

    monkeypatch.setattr(cases, "ElasticIngestion", FakeElastic)
    monkeypatch.setattr(
        cases,
        "data_service",
        SimpleNamespace(get_findings_by_case=lambda case_id: state.findings),
    )
    return state


def _sync(status="closed"):
    asyncio.run(cases._sync_upstream_status("c1", status))


def test_only_findings_with_an_elastic_alert_are_pushed(upstream):
    upstream.findings = [
        {"data_source": "elastic", "metadata": {"elastic_alert_id": "a1"}},
        {"data_source": "opensearch", "metadata": {"opensearch_finding_id": "o1"}},
        {"data_source": "elastic", "metadata": None},
        {"data_source": "elastic", "metadata": {"elastic_alert_id": "a2"}},
    ]

    _sync("investigating")

    assert upstream.calls == [("a1", "investigating"), ("a2", "investigating")]


def test_an_accepted_update_is_logged_as_synced(upstream, caplog):
    upstream.findings = [{"metadata": {"elastic_alert_id": "a1"}}]

    with caplog.at_level(logging.INFO, logger=cases.__name__):
        _sync()

    assert any(
        r.levelno == logging.INFO and "Synced" in r.message for r in caplog.records
    )


def test_a_refused_update_is_a_warning_and_not_synced(upstream, caplog):
    upstream.accepts = False
    upstream.findings = [{"metadata": {"elastic_alert_id": "a1"}}]

    with caplog.at_level(logging.INFO, logger=cases.__name__):
        _sync()

    assert upstream.calls == [("a1", "closed")]
    assert not any("Synced" in r.message for r in caplog.records)
    assert any(
        r.levelno == logging.WARNING and "a1" in r.message for r in caplog.records
    )
