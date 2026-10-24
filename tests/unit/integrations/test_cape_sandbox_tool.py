"""Unit tests for core/integrations/cape_sandbox/tool.py.

We test the tool dispatcher with ``httpx`` fully mocked. No CAPE instance required.
"""

import json

import pytest

import core.integrations.cape_sandbox.tool as cape


@pytest.mark.unit
class TestCallTool:
    @pytest.mark.asyncio
    async def test_missing_config(self, monkeypatch):
        monkeypatch.setattr(cape, "_load_config", lambda: {"url": "", "api_key": ""})
        out = await cape.handle_call_tool("cape_search_hash", {"hash": "a" * 64})
        body = json.loads(out[0].text)
        assert "error" in body

    @pytest.mark.asyncio
    async def test_search_hash_found_sha256(self, monkeypatch):
        monkeypatch.setattr(
            cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
        )

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": [{"id": 42, "target": "f.exe"}]}

        calls = []

        def fake_get(url, headers=None, timeout=None, params=None):
            calls.append(url)
            return FakeResp()

        monkeypatch.setattr(cape.httpx, "get", fake_get)

        out = await cape.handle_call_tool("cape_search_hash", {"hash": "a" * 64})
        body = json.loads(out[0].text)
        assert body["found"] is True
        assert body["hash_type"] == "sha256"
        assert body["tasks"][0]["id"] == 42
        # ensure we hit the sha256 path first
        assert "/sha256/" in calls[0]

    @pytest.mark.asyncio
    async def test_search_hash_not_found(self, monkeypatch):
        monkeypatch.setattr(
            cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
        )

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": []}

        monkeypatch.setattr(cape.httpx, "get", lambda *a, **kw: FakeResp())

        out = await cape.handle_call_tool("cape_search_hash", {"hash": "a" * 64})
        body = json.loads(out[0].text)
        assert body["found"] is False

    @pytest.mark.asyncio
    async def test_get_iocs_returns_extracted(self, monkeypatch):
        monkeypatch.setattr(
            cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
        )

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {
                        "network": {"hosts": [{"ip": "1.1.1.1"}]},
                        "dropped": [],
                    }
                }

        monkeypatch.setattr(cape.httpx, "get", lambda *a, **kw: FakeResp())

        out = await cape.handle_call_tool("cape_get_iocs", {"task_id": "5"})
        body = json.loads(out[0].text)
        assert body["iocs"]["ip"] == ["1.1.1.1"]

    @pytest.mark.asyncio
    async def test_unknown_tool(self, monkeypatch):
        monkeypatch.setattr(
            cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
        )
        out = await cape.handle_call_tool("does_not_exist", {})
        body = json.loads(out[0].text)
        assert "Unknown tool" in body["error"]
