"""Rejected vendor calls (401/403/429) are counted, warned about, and acted on."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from services.daemon import sandbox_poller, vendor_errors
from services.daemon.config import MetricsConfig, ProcessingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.processor import FindingProcessor
from services.daemon.sandbox_poller import SandboxPoller
from services.daemon.sandbox_submitter import SandboxSettings, SandboxSubmitter

pytestmark = pytest.mark.unit


def _resp(status, headers=None, body=None):
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        json=lambda: body if body is not None else {},
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    vendor_errors.reset_vendor_errors()
    clock = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(vendor_errors, "_now", lambda: clock.t)
    yield clock
    vendor_errors.reset_vendor_errors()


def _fail_if_called(*_a, **_kw):
    raise AssertionError("no HTTP call expected")


def test_warning_throttled_per_vendor_status_but_counts_keep_growing(
    _clean_state, caplog
):
    clock = _clean_state
    with caplog.at_level(logging.WARNING, logger=vendor_errors.logger.name):
        for _ in range(3):
            vendor_errors.record_vendor_error("shodan", 401)
        vendor_errors.record_vendor_error("shodan", 403)
        assert len([r for r in caplog.records if "shodan" in r.message]) == 2
        assert "revoked or expired" in caplog.records[0].message

        clock.t += 3601
        vendor_errors.record_vendor_error("shodan", 401)
        assert len(caplog.records) == 3

    snap = vendor_errors.vendor_error_snapshot()
    assert snap["shodan"] == {"total": 5, "by_status": {"401": 4, "403": 1}}


@pytest.mark.asyncio
async def test_processor_429_cools_down_shodan_and_vt(monkeypatch, _clean_state):
    clock = _clean_state
    processor = FindingProcessor(ProcessingConfig(auto_enrich_enabled=False))
    processor._enrichment_services = {
        "shodan": {"enabled": True, "api_key": "k"},
        "virustotal": {"enabled": True, "api_key": "k"},
    }
    calls = []

    def fake_get(url, **_kw):
        calls.append(url)
        if "shodan" in url:
            return _resp(429, {"Retry-After": "30"})
        return _resp(401)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert await processor._enrich_ip("203.0.113.7") is None
    assert len(calls) == 2

    # Shodan is cooling down, VT is not (401 starts no cool-down).
    assert await processor._enrich_hash("a" * 64) is None
    assert [u for u in calls if "shodan" in u] == calls[:1]
    assert len(calls) == 3

    monkeypatch.setattr(httpx, "get", _fail_if_called)
    clock.t += 10
    assert vendor_errors.vendor_cooling_down("shodan")
    clock.t += 21
    assert not vendor_errors.vendor_cooling_down("shodan")

    snap = vendor_errors.vendor_error_snapshot()
    assert snap["shodan"]["by_status"] == {"429": 1}
    assert snap["virustotal"]["by_status"] == {"401": 2}


@pytest.mark.asyncio
async def test_submitter_401_is_an_error_not_unknown_and_429_skips(monkeypatch):
    settings = SandboxSettings(
        auto_submit=True,
        max_file_size_mb=10,
        allowed_types=["exe"],
        timeout_seconds=300,
        joe_enabled=False,
        cape_enabled=False,
        hybrid_enabled=True,
        anyrun_enabled=False,
    )
    monkeypatch.setattr(
        "services.daemon.sandbox_submitter.get_integration_config",
        lambda _n: {"api_key": "k"},
    )
    submitter = SandboxSubmitter(settings)

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _resp(401))
    out = await submitter.submit_hash("a" * 64)
    assert out["hybrid_analysis"]["status"] == "error"
    assert out["hybrid_analysis"]["error"] == "http_401"

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _resp(429))
    await submitter.submit_hash("a" * 64)
    monkeypatch.setattr(httpx, "post", _fail_if_called)
    out = await submitter.submit_hash("a" * 64)
    assert out["hybrid_analysis"]["reason"] == "rate_limited"


class _DataService:
    def __init__(self, findings):
        self.findings = findings
        self.updates = []

    def get_findings(self):
        return self.findings

    def update_finding(self, finding_id, **kwargs):
        self.updates.append((finding_id, kwargs))
        return True


def _finding(sandbox="anyrun"):
    return {
        "finding_id": "f-1",
        "ai_enrichment": {
            "enrichment": {
                "sandbox_submissions": {
                    "h1": {
                        sandbox: {
                            "task_id": "t1",
                            "status": "cached",
                            "submitted_at": sandbox_poller.utcnow().isoformat(),
                        }
                    }
                }
            }
        },
    }


@pytest.mark.asyncio
async def test_poller_401_fails_sub_persists_and_is_not_refetched(monkeypatch):
    monkeypatch.setattr(
        sandbox_poller, "get_integration_config", lambda _n: {"api_key": "k"}
    )
    finding = _finding()
    ds = _DataService([finding])
    calls = []

    def fake_get(url, **_kw):
        calls.append(url)
        return _resp(401)

    monkeypatch.setattr(httpx, "get", fake_get)

    stats = await SandboxPoller(data_service=ds).run_once()
    assert stats["failed"] == 1 and len(calls) == 1
    sub = finding["ai_enrichment"]["enrichment"]["sandbox_submissions"]["h1"]["anyrun"]
    assert sub["status"] == "failed" and sub["error"] == "http_401"
    assert len(ds.updates) == 1

    monkeypatch.setattr(httpx, "get", _fail_if_called)
    stats = await SandboxPoller(data_service=ds).run_once()
    assert stats["failed"] == 0 and len(ds.updates) == 1


@pytest.mark.asyncio
async def test_poller_cape_401_without_api_key_still_counts(monkeypatch):
    monkeypatch.setattr(sandbox_poller, "get_secret", lambda _n: "")
    monkeypatch.setattr(
        sandbox_poller,
        "get_settings",
        lambda: SimpleNamespace(
            cape_sandbox_url="http://cape.test", sandbox_analysis_timeout=3600
        ),
    )
    ds = _DataService([_finding("cape")])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(401))
    stats = await SandboxPoller(data_service=ds).run_once()
    assert stats["failed"] == 1
    assert vendor_errors.vendor_error_snapshot()["cape"]["by_status"] == {"401": 1}


@pytest.mark.asyncio
async def test_poller_429_is_not_ready_not_failure(monkeypatch):
    monkeypatch.setattr(
        sandbox_poller, "get_integration_config", lambda _n: {"api_key": "k"}
    )
    ds = _DataService([_finding()])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _resp(429))
    stats = await SandboxPoller(data_service=ds).run_once()
    assert stats["failed"] == 0 and stats["errors"] == 0 and not ds.updates

    monkeypatch.setattr(httpx, "get", _fail_if_called)
    await SandboxPoller(data_service=ds).run_once()


@pytest.mark.asyncio
async def test_status_endpoint_includes_vendor_counts():
    vendor_errors.record_vendor_error("virustotal", 401)
    server = MetricsServer(MetricsConfig())
    body = (await server._handle_status(None)).text
    assert '"vendors": {"virustotal": {"total": 1, "by_status": {"401": 1}}}' in body
