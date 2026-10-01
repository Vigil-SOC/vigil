"""Jira export authenticates with the descriptor's username and api_token.

get_integration_config strips secrets before they are stored, and the Jira
descriptor names the account field username, not email. Both export routes
have to read through resolve.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services.api.routers.jira_export import (
    JiraExportRequest,
    JiraRemediationExportRequest,
    export_case_to_jira,
    export_remediation_to_jira,
)

pytestmark = pytest.mark.unit


class _Query:
    def __init__(self, result):
        self.result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.result

    def all(self):
        return self.result if isinstance(self.result, list) else [self.result]


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.is_success = True

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _user():
    return SimpleNamespace(
        user_id="user-1",
        username="ada",
        email="ada@example.com",
        full_name="Ada",
    )


def _case():
    return SimpleNamespace(
        case_id="case-9",
        title="Beacon",
        priority="high",
        status="open",
        created_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        assignee="ada",
        description="periodic beacon",
        metadata={"resolution_steps": [{"description": "Block the host", "action_taken": "blocked", "result": "ok"}]},
        findings=[],
    )


def test_export_authenticates_with_resolved_username(monkeypatch):
    captured = {}

    def fake_post(url, auth=None, json=None, **_kwargs):
        captured.setdefault("posts", []).append({"url": url, "auth": auth, "json": json})
        return _Response({"key": "SEC-1"})

    monkeypatch.setattr(
        "services.api.routers.jira_export.resolve",
        lambda descriptor: {
            "url": "https://jira.example",
            "username": "ada",
            "api_token": "tok",
            "project_key": "FROM-CONFIG",
        }
        if descriptor.id == "jira"
        else {},
    )
    monkeypatch.setattr("services.api.routers.jira_export.httpx.post", fake_post)
    monkeypatch.setattr(
        "services.api.routers.jira_export.AuthService.check_permission",
        lambda *_args, **_kwargs: True,
    )

    class Session:
        def query(self, model):
            if getattr(model, "__name__", "") == "Case":
                return _Query(_case())
            return _Query([])

    result = export_case_to_jira(
        "case-9",
        JiraExportRequest(project_key="FROM-BODY"),
        _user(),
        Session(),
    )

    assert result.success is True
    assert captured["posts"][0]["auth"] == ("ada", "tok")
    assert captured["posts"][0]["json"]["fields"]["project"]["key"] == "FROM-BODY"


def test_remediation_export_authenticates_with_resolved_username(monkeypatch):
    captured = {}

    def fake_get(url, auth=None, **_kwargs):
        captured["get_auth"] = auth
        return _Response({"fields": {"project": {"key": "SEC"}}})

    def fake_post(url, auth=None, **_kwargs):
        captured["post_auth"] = auth
        return _Response({"key": "SEC-2"})

    monkeypatch.setattr(
        "services.api.routers.jira_export.resolve",
        lambda _descriptor: {
            "url": "https://jira.example",
            "username": "ada",
            "api_token": "tok",
            "project_key": "SEC",
        },
    )
    monkeypatch.setattr("services.api.routers.jira_export.httpx.get", fake_get)
    monkeypatch.setattr("services.api.routers.jira_export.httpx.post", fake_post)
    monkeypatch.setattr(
        "services.api.routers.jira_export.AuthService.check_permission",
        lambda *_args, **_kwargs: True,
    )

    class Session:
        def query(self, model):
            if getattr(model, "__name__", "") == "Case":
                return _Query(_case())
            return _Query([])

    result = export_remediation_to_jira(
        "case-9",
        JiraRemediationExportRequest(parent_issue_key="SEC-1"),
        _user(),
        Session(),
    )

    assert result.success is True
    assert captured["get_auth"] == ("ada", "tok")
    assert captured["post_auth"] == ("ada", "tok")
