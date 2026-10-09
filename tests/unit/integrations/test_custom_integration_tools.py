"""The generate response lists tools; a reply without them still parses."""

import json

import pytest

from core.integrations.custom_integration_service import CustomIntegrationService


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.integrations.custom_integration_service.vigil_path",
        lambda name: tmp_path / name,
    )
    return CustomIntegrationService()


def _reply(**extra):
    body = {"id": "acme", "name": "Acme", "metadata": {"fields": []}, "server_code": "x", **extra}
    return f"```json\n{json.dumps(body)}\n```"


def test_tools_default_to_empty(service):
    data = service._parse_claude_response(_reply(), "Custom")
    assert data["tools"] == []


def test_tools_keep_name_and_description_only(service):
    tools = [
        {"name": "acme_list", "description": "List findings", "read_only": True},
        {"name": "acme_get"},
        {"description": "no name"},
        "junk",
    ]
    data = service._parse_claude_response(_reply(tools=tools), "Custom")
    assert data["tools"] == [
        {"name": "acme_list", "description": "List findings"},
        {"name": "acme_get", "description": ""},
    ]
