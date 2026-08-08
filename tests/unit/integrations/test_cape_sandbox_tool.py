"""Unit tests for tools/cape_sandbox.py.

We test the pure helpers (IOC extraction from a CAPE report) and the tool
dispatcher with ``requests`` fully mocked. No CAPE instance required.
"""

import json

import pytest

import tools.cape_sandbox as cape


@pytest.mark.unit
class TestIocExtraction:
    def test_empty_report(self):
        out = cape._extract_iocs({})
        assert out["ips"] == []
        assert out["domains"] == []
        assert out["hashes"] == []

    def test_extracts_network_and_dropped(self):
        report = {
            "network": {
                "hosts": [{"ip": "8.8.8.8"}, "9.9.9.9"],
                "dns": [{"request": "bad.example"}, {"request": "bad.example"}],
                "http": [{"uri": "http://bad.example/p"}],
            },
            "dropped": [{"sha256": "z" * 64}],
            "behavior": {"summary": {"mutexes": ["m1"]}},
        }
        out = cape._extract_iocs(report)
        assert "8.8.8.8" in out["ips"]
        assert "9.9.9.9" in out["ips"]
        assert "bad.example" in out["domains"]
        # deduped
        assert out["domains"].count("bad.example") == 1
        assert "http://bad.example/p" in out["urls"]
        assert "z" * 64 in out["hashes"]
        assert "m1" in out["mutexes"]


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

        monkeypatch.setattr(cape.requests, "get", fake_get)

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

        monkeypatch.setattr(cape.requests, "get", lambda *a, **kw: FakeResp())

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

        monkeypatch.setattr(cape.requests, "get", lambda *a, **kw: FakeResp())

        out = await cape.handle_call_tool("cape_get_iocs", {"task_id": "5"})
        body = json.loads(out[0].text)
        assert "1.1.1.1" in body["iocs"]["ips"]

    @pytest.mark.asyncio
    async def test_unknown_tool(self, monkeypatch):
        monkeypatch.setattr(
            cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
        )
        out = await cape.handle_call_tool("does_not_exist", {})
        body = json.loads(out[0].text)
        assert "Unknown tool" in body["error"]
