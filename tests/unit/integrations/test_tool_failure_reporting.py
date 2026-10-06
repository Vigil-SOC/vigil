"""Handled tool failures are reported as failures, not as successful calls.

Covers the shared ``is_error`` mapping, the Entra token exchange wording, the
client/in-process failure logging, and the registry fallbacks that used to hide
at DEBUG.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from opentelemetry.trace import StatusCode

import core.integrations.azure_ad.tool as aad
import core.integrations.microsoft_defender.tool as mde_tool
from core.integrations.mcp import in_process
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.registry import live_mcp_tools, safe_tool_names

pytestmark = pytest.mark.unit

TOKEN_URL = "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
CONFIG = {"tenant_id": "tid", "client_id": "cid", "client_secret": "s3cret-value"}


def _params(name="mde_get_alerts", arguments=None):
    return SimpleNamespace(name=name, arguments=arguments or {})


def _stub_config(monkeypatch, module, config):
    monkeypatch.setattr(module, "resolve", lambda *a, **kw: config)


# --------------------------------------------------------------------- #
# is_error mapping
# --------------------------------------------------------------------- #


async def test_handled_failure_is_flagged_as_error(monkeypatch):
    _stub_config(monkeypatch, mde_tool, {})
    out = await mde_tool._on_call_tool(None, _params())
    assert out.is_error is True
    assert "not configured" in out.content[0].text


@respx.mock
async def test_success_payload_is_not_flagged(monkeypatch):
    _stub_config(monkeypatch, mde_tool, CONFIG)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at"})
    )
    respx.get("https://api.securitycenter.microsoft.com/api/alerts").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    out = await mde_tool._on_call_tool(None, _params())
    assert out.is_error is False
    assert json.loads(out.content[0].text) == {"count": 0, "alerts": []}


async def test_escaping_exception_is_still_an_error(monkeypatch):
    async def boom(name, arguments):
        raise RuntimeError("kaput")

    monkeypatch.setattr(mde_tool, "handle_call_tool", boom)
    out = await mde_tool._on_call_tool(None, _params())
    assert out.is_error is True
    assert out.content[0].text == "kaput"


# --------------------------------------------------------------------- #
# Rejected credential is not "not configured"
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module, label, call",
    [
        (mde_tool, "Microsoft Defender", ("mde_get_alerts", {})),
        (aad, "Azure AD", ("aad_get_user", {"user": "a@b.test"})),
    ],
)
@respx.mock
async def test_rejected_credential_is_logged_and_reported(
    monkeypatch, caplog, module, label, call
):
    _stub_config(monkeypatch, module, CONFIG)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            },
        )
    )
    with caplog.at_level(logging.WARNING):
        out = await module._on_call_tool(None, _params(*call))

    text = json.loads(out.content[0].text)["error"]
    assert out.is_error is True
    assert "rejected" in text and "HTTP 401" in text and "AADSTS7000215" in text
    assert "not configured" not in text
    record = next(r for r in caplog.records if "HTTP 401" in r.getMessage())
    assert "AADSTS7000215" in record.getMessage()
    assert "s3cret-value" not in caplog.text


@respx.mock
async def test_token_endpoint_outage_is_not_called_a_rejection(monkeypatch):
    _stub_config(monkeypatch, aad, CONFIG)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="down"))
    out = await aad._on_call_tool(None, _params("aad_get_user", {"user": "x"}))
    text = json.loads(out.content[0].text)["error"]
    assert "rejected" not in text and "HTTP 503" in text


# --------------------------------------------------------------------- #
# Client and in-process record failed calls
# --------------------------------------------------------------------- #


async def test_client_logs_and_marks_span_on_error_result(caplog):
    class _Service:
        servers = {"demo": object()}

    client = MCPClient(_Service())
    session = MagicMock()

    async def fake_call(tool, args):
        return {"error": True, "content": [{"type": "text", "text": "vendor said no"}]}

    session.call_tool = fake_call
    client.persistent_sessions["demo"] = session
    span = MagicMock()
    tracer = MagicMock()
    tracer.start_span.return_value = span

    with patch("core.telemetry.get_tracer", return_value=tracer), caplog.at_level(
        logging.WARNING
    ):
        result = await client.call_tool("demo", "do_it", {})

    assert result["error"] is True
    assert "demo.do_it" in caplog.text and "vendor said no" in caplog.text
    assert span.set_status.call_args.args[0] == StatusCode.ERROR
    span.set_attribute.assert_any_call("vigil.tool.success", False)


def test_in_process_logs_and_marks_span_on_error_result(caplog):
    class _Server:
        async def call_tool(self, name, args):
            return SimpleNamespace(
                is_error=True, content=[SimpleNamespace(text="bad input")]
            )

    span = MagicMock()
    span.is_recording.return_value = True
    with patch.object(in_process, "_server", lambda: _Server()), patch.object(
        in_process.trace, "get_current_span", return_value=span
    ), caplog.at_level(logging.WARNING):
        result = asyncio.run(in_process.call_tool("t", {}))

    assert result["error"] is True
    assert "bad input" in caplog.text
    assert span.set_status.call_args.args[0] == StatusCode.ERROR


# --------------------------------------------------------------------- #
# Registry fallbacks are no longer silent
# --------------------------------------------------------------------- #


def test_safe_tool_names_warns_when_registry_unreadable(caplog):
    registry = MagicMock()
    registry.get_tool_names.side_effect = RuntimeError("db down")
    with caplog.at_level(logging.WARNING):
        assert safe_tool_names(registry) == []
    assert any(
        r.levelno >= logging.WARNING and "db down" in r.getMessage()
        for r in caplog.records
    )


def test_live_mcp_tools_warns_when_surface_unreadable(caplog):
    registry = MagicMock()
    registry.get_all_tools.side_effect = RuntimeError("registry gone")
    with patch("core.integrations.mcp.registry.refresh_from_client"), caplog.at_level(
        logging.WARNING
    ):
        assert live_mcp_tools(registry) == []
    assert any(
        r.levelno >= logging.WARNING and "registry gone" in r.getMessage()
        for r in caplog.records
    )
