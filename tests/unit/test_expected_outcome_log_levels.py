"""Expected outcomes ("not found", unfinished config) must not log at ERROR.

See docs/logging-levels.md.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from core.cases.case_sla_service import CaseSLAService, SlaOutcome
from core.cases.case_workflow_service import CaseWorkflowService
from core.integrations._base import config_gap

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_config_gap():
    config_gap._skipped.clear()
    yield
    config_gap._skipped.clear()


def _session(*first_results):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = first_results
    return session


def _no_errors(caplog):
    # The SIEM base class logs unrelated DB health errors outside a database.
    errors = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and (r.name.startswith("core.integrations") or r.name.startswith("core.cases"))
    ]
    assert not errors


def test_sla_case_not_found_is_info(caplog):
    caplog.set_level(logging.DEBUG)
    result = CaseSLAService().assign_sla_to_case("c-1", session=_session(None))
    assert result.outcome is SlaOutcome.NO_SUCH_CASE
    _no_errors(caplog)


def test_sla_no_default_policy_is_warning(caplog):
    caplog.set_level(logging.DEBUG)
    case = MagicMock(priority="high")
    result = CaseSLAService().assign_sla_to_case(
        "c-1", session=_session(case, None, None)
    )
    assert result.outcome is SlaOutcome.NO_DEFAULT_POLICY
    _no_errors(caplog)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    "call",
    [
        lambda s, svc: svc.create_case_from_template("t", "title", session=s),
        lambda s, svc: svc.escalate_case("c", "a", "b", "why", session=s),
        lambda s, svc: svc.update_template("t", {}, session=s),
        lambda s, svc: svc.delete_template("t", session=s),
    ],
)
def test_workflow_not_found_is_info(call, caplog):
    caplog.set_level(logging.DEBUG)
    call(_session(None), CaseWorkflowService())
    _no_errors(caplog)
    assert any("not found" in r.getMessage() for r in caplog.records)


def _sentinel(config):
    from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

    with patch(
        "core.integrations.azure_sentinel.ingestion.resolve", return_value=config
    ):
        return AzureSentinelIngestion()


def _fake_azure(monkeypatch):
    import sys
    import types

    identity = types.ModuleType("azure.identity")
    identity.ClientSecretCredential = MagicMock()
    insights = types.ModuleType("azure.mgmt.securityinsight")
    insights.SecurityInsights = MagicMock()
    for name, module in {
        "azure": types.ModuleType("azure"),
        "azure.identity": identity,
        "azure.mgmt": types.ModuleType("azure.mgmt"),
        "azure.mgmt.securityinsight": insights,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def _defender(config):
    from core.integrations.microsoft_defender.ingestion import (
        MicrosoftDefenderIngestion,
    )

    with patch(
        "core.integrations.microsoft_defender.ingestion.resolve",
        return_value=config,
    ):
        return MicrosoftDefenderIngestion()


def _elastic(config):
    from core.integrations.elastic.ingestion import ElasticIngestion

    with patch("core.integrations.elastic.ingestion.resolve", return_value=config):
        return ElasticIngestion()


def _opensearch(config):
    from core.integrations.opensearch.ingestion import OpenSearchIngestion

    with patch("core.integrations.opensearch.ingestion.resolve", return_value=config):
        return OpenSearchIngestion()


SENTINEL_FULL = {
    k: "x"
    for k in (
        "tenant_id",
        "client_id",
        "client_secret",
        "subscription_id",
        "resource_group",
        "workspace_name",
    )
}

# name, factory, incomplete config, complete config
CASES = [
    ("sentinel", _sentinel, {}, SENTINEL_FULL),
    (
        "defender",
        _defender,
        {},
        {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    ),
    (
        "elastic",
        _elastic,
        {"elasticsearch_url": None},
        {"elasticsearch_url": "http://es"},
    ),
    (
        "opensearch",
        _opensearch,
        {"opensearch_url": None},
        {"opensearch_url": "http://os"},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,factory,incomplete,complete", CASES)
async def test_incomplete_config_logs_once_then_recovers(
    name, factory, incomplete, complete, caplog, monkeypatch
):
    _fake_azure(monkeypatch)
    caplog.set_level(logging.DEBUG)

    for _ in range(5):
        # Federation builds a fresh instance every tick.
        assert await factory(incomplete).fetch_alerts() == []
    gap = [r for r in caplog.records if "incomplete" in r.getMessage()]
    assert len(gap) == 1
    assert gap[0].levelno == logging.WARNING
    _no_errors(caplog)

    caplog.clear()
    try:
        await factory(complete).fetch_alerts()
    except Exception:
        pass  # the recovery line comes before any vendor call
    recovered = [r for r in caplog.records if "polling resumed" in r.getMessage()]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO


def test_reminder_every_n_skipped_polls(caplog):
    log = logging.getLogger("t")
    caplog.set_level(logging.DEBUG)
    for _ in range(config_gap.REMINDER_EVERY * 2):
        config_gap.report_config_gap(log, "X", ["field_a"])
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 3  # start + two reminders
    assert "field_a" in msgs[0] and "still incomplete" in msgs[1]
