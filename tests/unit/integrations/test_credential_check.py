"""check_credentials reads the saved URL and credentials, read-only, per integration."""

from __future__ import annotations

import pytest

from core.integrations import credential_check

pytestmark = pytest.mark.unit


def _resolved(monkeypatch, **fields):
    monkeypatch.setattr(credential_check, "resolve", lambda descriptor: fields)


async def test_integration_without_a_check_is_left_to_the_mcp_probe():
    assert await credential_check.check_credentials("virustotal") is None


async def test_splunk_without_a_password_says_what_is_missing(monkeypatch):
    _resolved(monkeypatch, server_url="https://s:8089", username="u", password=None)

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Missing password",
    )


async def test_splunk_reports_what_the_login_said(monkeypatch):
    _resolved(
        monkeypatch,
        server_url="https://s:8089",
        username="u",
        password="wrong",
        verify_ssl=True,
        ca_cert_path=None,
    )
    seen = {}

    def test_connection(self):
        seen["url"], seen["password"] = self.server_url, self.password
        return False, "Authentication failed"

    monkeypatch.setattr(
        "core.integrations.splunk.client.SplunkService.test_connection",
        test_connection,
    )

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Authentication failed",
    )
    assert seen == {"url": "https://s:8089", "password": "wrong"}


async def test_elastic_without_a_url_says_so(monkeypatch):
    _resolved(monkeypatch, elasticsearch_url=None)

    assert await credential_check.check_credentials("elastic-siem") == (
        False,
        "Missing elasticsearch_url",
    )


async def test_elastic_unreachable_url_fails_with_the_connection_error(monkeypatch):
    # nothing listens on port 1: a real refused connection, no mock of the client
    _resolved(
        monkeypatch,
        elasticsearch_url="http://127.0.0.1:1",
        api_key="not-a-key",
        verify_ssl=None,
    )

    ok, message = await credential_check.check_credentials("elastic-siem")

    assert ok is False
    assert message


async def test_a_check_that_blows_up_is_a_failure_that_names_no_value(monkeypatch):
    async def boom():
        raise RuntimeError("http://user:hunter2@host")

    monkeypatch.setitem(credential_check._CHECKS, "splunk", boom)

    assert await credential_check.check_credentials("splunk") == (
        False,
        "Could not check the connection (RuntimeError)",
    )
