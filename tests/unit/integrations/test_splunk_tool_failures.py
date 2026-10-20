"""splunk_execute reports Splunk's own reason, and only a deadline reads as a timeout.

The agent layer classifies a failed MCP call by scanning the payload text
(mcp_tools._failure_kind): "timed out" is a visibility gap, anything else a
backend defect. A bad search must therefore never carry that wording, including
through an echoed query.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.agents.mcp_tools import BACKEND_ERROR, TIMEOUT, _failure_kind
from core.integrations.splunk import tool as splunk_tool
from core.integrations.splunk.client import SplunkService

pytestmark = pytest.mark.unit

BASE = "https://splunk.example.com:8089"
JOBS = f"{BASE}/services/search/jobs"


@pytest.fixture(autouse=True)
def _splunk(monkeypatch):
    service = SplunkService(BASE, "u", "p")
    monkeypatch.setattr(splunk_tool, "get_splunk_service", lambda: service)
    monkeypatch.setattr("core.integrations.splunk.client._POLL_INTERVAL_S", 0.01)
    respx.post(f"{BASE}/services/auth/login").mock(
        return_value=httpx.Response(200, json={"sessionKey": "sk"})
    )


async def _call(spl: str, tool: str = "splunk_execute") -> dict:
    args = {"spl_query": spl} if tool == "splunk_execute" else {"query": spl}
    out = await splunk_tool.handle_call_tool(tool, args)
    return json.loads(out[0].text)


def _job(content: dict) -> None:
    respx.post(JOBS).mock(return_value=httpx.Response(201, json={"sid": "s1"}))
    respx.get(f"{JOBS}/s1").mock(
        return_value=httpx.Response(200, json={"entry": [{"content": content}]})
    )
    respx.get(f"{JOBS}/s1/results").mock(
        return_value=httpx.Response(200, json={"results": [{"n": "1"}]})
    )
    respx.delete(f"{JOBS}/s1").mock(return_value=httpx.Response(200))


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("spl", ["index=main | statz count", "index=timeout | statz"])
async def test_bad_search_surfaces_splunks_message_as_a_backend_error(spl):
    respx.post(JOBS).mock(
        return_value=httpx.Response(
            400,
            json={
                "messages": [
                    {"type": "FATAL", "text": "Unknown search command 'statz'."}
                ]
            },
        )
    )
    payload = await _call(spl)

    assert "Unknown search command 'statz'" in payload["error"]
    assert spl not in json.dumps(payload)
    assert _failure_kind(json.dumps(payload)) == BACKEND_ERROR


@respx.mock
@pytest.mark.asyncio
async def test_failed_job_is_a_backend_error():
    _job(
        {
            "isDone": True,
            "isFailed": True,
            "messages": [{"type": "FATAL", "text": "Error in 'stats' command"}],
        }
    )
    payload = await _call("index=timeout | stats")

    assert "Error in 'stats' command" in payload["error"]
    assert _failure_kind(json.dumps(payload)) == BACKEND_ERROR


@respx.mock
@pytest.mark.asyncio
async def test_poll_deadline_is_the_only_timeout(monkeypatch):
    monkeypatch.setattr("core.integrations.splunk.client.SEARCH_DEADLINE_S", 0.05)
    _job({"isDone": False})

    payload = await _call("index=main")

    assert "timed out" in payload["error"]
    assert _failure_kind(json.dumps(payload)) == TIMEOUT


@respx.mock
@pytest.mark.asyncio
async def test_success_carries_event_count_and_warnings():
    _job(
        {
            "isDone": True,
            "eventCount": 7,
            "messages": [{"type": "WARN", "text": "Field 'foo' not found"}],
        }
    )
    payload = await _call("index=main | stats count")

    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["event_count"] == 7
    assert payload["warnings"] == ["Field 'foo' not found"]


@respx.mock
@pytest.mark.asyncio
async def test_success_omits_warnings_when_there_are_none():
    _job({"isDone": True, "eventCount": 1})

    assert "warnings" not in await _call("index=main")


@respx.mock
@pytest.mark.asyncio
async def test_other_tools_return_the_reason_too():
    respx.post(JOBS).mock(
        return_value=httpx.Response(
            400, json={"messages": [{"type": "FATAL", "text": "bad things"}]}
        )
    )
    nl = await _call("failed login", "splunk_nl_search")
    ip = await splunk_tool.handle_call_tool(
        "splunk_search_ip", {"ip_address": "1.2.3.4"}
    )
    host = await splunk_tool.handle_call_tool("splunk_search_host", {"hostname": "h"})
    for text in (json.dumps(nl), ip[0].text, host[0].text):
        assert "bad things" in json.loads(text)["error"]
