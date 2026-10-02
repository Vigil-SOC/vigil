"""Home setup steps are a read over config that already exists."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.storage.models import AIModelConfig, CustomAgent

pytestmark = pytest.mark.unit


def _body(**overrides):
    from services.api.routers.config import build_setup_steps

    args = {
        "loaded": {"configured": True, "integrations": {}, "enabled_integrations": []},
        "secrets_set": {},
        "sources": [{"status": "not_cloned", "rule_count": 0}],
        "model_ids": set(),
        "descriptor_count": 4,
        "alerts_exist": 0,
        "demo_enabled": False,
    }
    args.update(overrides)
    return build_setup_steps(**args)


def _step(body, step_id):
    return next(step for step in body["steps"] if step["id"] == step_id)


def test_empty_integrations_file_stays_open():
    body = _body(
        loaded={"configured": True, "integrations": {}, "enabled_integrations": []}
    )
    step = _step(body, "connect_tools")
    assert step["done"] is False
    assert step["state_line"] == "0 of 4 integrations connected"
    assert step["href"] == "/settings?section=integrations"
    assert [item["id"] for item in body["steps"]] == [
        "connect_tools",
        "notify",
        "rules",
        "per_agent",
    ]


def test_saved_integration_counts_whether_or_not_it_is_enabled():
    body = _body(
        loaded={
            "configured": True,
            "integrations": {"splunk": {"server_url": "https://splunk"}},
            "enabled_integrations": [],
        }
    )
    step = _step(body, "connect_tools")
    assert step["done"] is True
    assert step["state_line"] == "1 of 4 integrations connected"


def test_null_slack_flag_and_pagerduty_integration_key_stay_open():
    body = _body(
        secrets_set={
            "slack": {"bot_token": None},
            "pagerduty": {"api_token": False, "integration_key": True},
        }
    )
    step = _step(body, "notify")
    assert step["done"] is False
    assert step["state_line"] == "No Slack or PagerDuty route yet"


def test_slack_or_pagerduty_token_closes_notify():
    slack = _step(_body(secrets_set={"slack": {"bot_token": True}}), "notify")
    pagerduty = _step(_body(secrets_set={"pagerduty": {"api_token": True}}), "notify")
    assert slack["done"] is True
    assert pagerduty["done"] is True
    assert slack["href"] == "/settings?section=integrations"


def test_not_cloned_source_stays_open_until_rules_are_ready():
    open_step = _step(
        _body(sources=[{"status": "not_cloned", "rule_count": 12}]), "rules"
    )
    empty_ready = _step(_body(sources=[{"status": "ready", "rule_count": 0}]), "rules")
    ready = _step(_body(sources=[{"status": "ready", "rule_count": 3}]), "rules")
    assert open_step["done"] is False
    assert open_step["state_line"] == "No detection rules on disk"
    assert empty_ready["done"] is False
    assert ready["done"] is True
    assert ready["href"] == "/settings?section=integrations&tab=detection"


def test_per_agent_needs_two_distinct_models():
    none = _step(_body(model_ids=set()), "per_agent")
    one = _step(_body(model_ids={"gemini/gemini-flash-latest"}), "per_agent")
    two = _step(
        _body(model_ids={"gemini/gemini-flash-latest", "claude-sonnet"}),
        "per_agent",
    )
    assert none["done"] is False
    assert none["state_line"] == "No model assigned"
    assert one["done"] is False
    assert one["state_line"] == "All agents use one model"
    assert two["done"] is True
    assert none["href"] == "/settings?section=ai-config"


def test_alerts_exist_is_the_findings_count():
    body = _body(alerts_exist=7, demo_enabled=True)
    assert body["alerts_exist"] == 7
    assert body["demo_enabled"] is True


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self):
        self.queried = []

    def query(self, column):
        self.queried.append(column)
        if column is AIModelConfig.model_id:
            return _Rows([("alpha",), ("",), (None,), ("  beta ",)])
        if column is CustomAgent.model:
            return _Rows([("alpha",), ("   ",), ("gamma",)])
        raise AssertionError(f"unexpected column {column}")


def test_assigned_models_skip_blanks_and_fallback():
    from services.api.routers.config import assigned_model_ids

    session = _Session()
    assert assigned_model_ids(session) == {"alpha", "beta", "gamma"}
    assert session.queried == [AIModelConfig.model_id, CustomAgent.model]


def test_endpoint_uses_the_existing_reads():
    from services.api.routers import config as config_module

    loaded = {
        "configured": True,
        "enabled_integrations": ["slack"],
        "integrations": {"slack": {}, "pagerduty": {}},
    }
    rules = MagicMock()
    rules.list_sources.return_value = [{"status": "not_cloned", "rule_count": 0}]
    findings = MagicMock()
    findings.count_findings.return_value = 7

    def fake_secret(key):
        if key == "PAGERDUTY_INTEGRATION_KEY":
            return "routing-key"
        return None

    with (pytest.MonkeyPatch.context() as patch,):
        patch.setattr(config_module, "load_integrations_config", lambda _svc: loaded)
        patch.setattr(config_module, "get_config_service", lambda: object())
        patch.setattr(config_module, "get_secret", fake_secret)
        patch.setattr(config_module, "iter_descriptors", lambda: (object(), object()))
        patch.setattr(config_module, "is_demo_mode", lambda: False)
        patch.setattr(config_module, "findings_data_service", findings)
        patch.setattr(config_module, "assigned_model_ids", lambda _session: set())

        body = asyncio.run(
            config_module.get_setup_steps(session=object(), detection_rules=rules)
        )

    assert _step(body, "connect_tools")["state_line"] == "2 of 2 integrations connected"
    assert _step(body, "connect_tools")["done"] is True
    notify = _step(body, "notify")
    assert notify["done"] is False
    assert notify["state_line"] == "No Slack or PagerDuty route yet"
    assert _step(body, "rules")["done"] is False
    assert body["alerts_exist"] == 7
    assert body["demo_enabled"] is False
    findings.count_findings.assert_called_once_with()
