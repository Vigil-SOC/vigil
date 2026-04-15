"""Tests for the Darktrace inbound webhook receiver."""

import hashlib
import hmac
import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Mirror backend/main.py's sys.path setup so intra-package imports like
# `from api.foo import ...` in backend/api/__init__.py resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load the receiver module directly (bypassing backend/api/__init__.py which
# eagerly imports many heavy modules not needed for this test).
_spec = importlib.util.spec_from_file_location(
    "darktrace_webhook_under_test",
    _BACKEND_DIR / "api" / "darktrace_webhook.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["darktrace_webhook_under_test"] = _mod
_spec.loader.exec_module(_mod)
darktrace_router = _mod.router

from services.darktrace_ingestion import DarktraceIngestionService  # noqa: E402

SECRET = "unit-test-secret"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DARKTRACE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("DARKTRACE_URL", "https://dt.example.com")
    _app = FastAPI()
    _app.include_router(darktrace_router, prefix="/api/webhooks/darktrace")
    return _app


@pytest.fixture()
def client(app):
    return TestClient(app)


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post(client, path: str, payload: dict, sig: str | None = None) -> "object":
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if sig is None:
        sig = _sign(body)
    if sig:
        headers["X-Darktrace-Signature"] = sig
    return client.post(path, content=body, headers=headers)


MODEL_BREACH_SAMPLE = {
    "pbid": 4242,
    "time": 1712995200000,
    "score": 0.87,
    "model": {
        "name": "Device / Anomalous Connection",
        "tags": [{"name": "T1071.001"}, {"name": "T1048"}],
    },
    "device": {"ip": "10.0.0.5", "hostname": "laptop-01", "mac": "aa:bb:cc:dd:ee:ff"},
    "destinationIp": "203.0.113.9",
}

AI_ANALYST_SAMPLE = {
    "uuid": "e4c1d7ad-1111-4222-8333-9ab00c0de01e",
    "title": "Possible Command & Control Activity",
    "createdAt": "2026-04-01T12:34:56Z",
    "groupScore": 92,
    "mitreTactics": ["T1071"],
    "breachDevices": [{"ip": "10.0.0.5", "hostname": "laptop-01"}],
}

SYSTEM_STATUS_SAMPLE = {
    "id": "sys-123",
    "name": "Probe Disconnected",
    "status": "warning",
    "time": 1712995200000,
    "message": "Probe dt-probe-1 lost contact for 5m",
}


# ---------------------------------------------------------------------------
# Transform unit tests (no network, no ingestion)
# ---------------------------------------------------------------------------


class TestTransforms:
    def test_model_breach_maps_core_fields(self):
        svc = DarktraceIngestionService(console_url="https://dt.example.com")
        f = svc.transform_model_breach(MODEL_BREACH_SAMPLE)
        assert f is not None
        assert re.match(r"^f-\d{8}-[a-f0-9]{8}$", f["finding_id"])
        assert f["data_source"] == "darktrace"
        assert f["anomaly_score"] == pytest.approx(0.87)
        assert f["severity"] == "high"
        assert f["entity_context"] == {
            "src_ip": "10.0.0.5",
            "hostname": "laptop-01",
            "mac": "aa:bb:cc:dd:ee:ff",
            "dst_ip": "203.0.113.9",
        }
        assert f["mitre_predictions"] == {"T1071.001": 0.7, "T1048": 0.7}
        assert f["evidence_links"][0]["ref"].endswith("/#modelbreach/4242")
        assert len(f["embedding"]) == 768

    def test_model_breach_idempotent_finding_id(self):
        svc = DarktraceIngestionService()
        a = svc.transform_model_breach(MODEL_BREACH_SAMPLE)
        b = svc.transform_model_breach(MODEL_BREACH_SAMPLE)
        assert a["finding_id"] == b["finding_id"]

    def test_model_breach_missing_pbid_returns_none(self):
        svc = DarktraceIngestionService()
        assert svc.transform_model_breach({"score": 0.5}) is None

    def test_ai_analyst_maps_score_and_mitre(self):
        svc = DarktraceIngestionService(console_url="https://dt.example.com")
        f = svc.transform_ai_analyst(AI_ANALYST_SAMPLE)
        assert f is not None
        # groupScore 92 is on 0-100 scale -> clamped to 0.92
        assert f["anomaly_score"] == pytest.approx(0.92)
        assert f["severity"] == "critical"
        assert f["mitre_predictions"] == {"T1071": 0.7}
        assert f["entity_context"]["src_ip"] == "10.0.0.5"
        assert "aianalyst/incident" in f["evidence_links"][0]["ref"]

    def test_ai_analyst_zero_groupscore_preserved(self):
        """Regression: groupScore=0 must map to anomaly_score=0.0 / info.

        A legitimate zero (Darktrace's minimum criticality) used to fall
        through the ``or``-chain to the 0.5 default, inflating a zero-
        criticality event to medium severity.
        """
        svc = DarktraceIngestionService()
        payload = {
            "uuid": "zero-score-incident",
            "title": "Low Noise Event",
            "createdAt": "2026-04-01T00:00:00Z",
            "groupScore": 0,
        }
        f = svc.transform_ai_analyst(payload)
        assert f is not None
        assert f["anomaly_score"] == pytest.approx(0.0)
        assert f["severity"] == "info"

    def test_system_status_always_informational(self):
        svc = DarktraceIngestionService()
        f = svc.transform_system_status(SYSTEM_STATUS_SAMPLE)
        assert f is not None
        assert f["anomaly_score"] == pytest.approx(0.2)
        assert f["severity"] == "medium"  # "warning" -> medium via normalize
        assert f["data_source"] == "darktrace"

    def test_system_status_fallback_finding_id_is_deterministic(self):
        """Regression: fallback key must be stable across processes.

        Previously used ``hash(frozenset(...))``, which is randomized per
        process (PYTHONHASHSEED) and would silently duplicate findings
        across worker restarts. Simulate a second process by patching the
        builtin ``hash`` to ensure the output no longer depends on it.
        """
        svc = DarktraceIngestionService()
        payload = {
            "status": "warning",
            "time": 1712995200000,
            "message": "Probe lost",
        }
        first = svc.transform_system_status(payload)

        # Monkeypatch the builtin hash to prove determinism doesn't rely on it.
        import builtins

        original_hash = builtins.hash
        try:
            builtins.hash = lambda _obj: 424242  # noqa: E731
            second = svc.transform_system_status(payload)
        finally:
            builtins.hash = original_hash

        assert first["finding_id"] == second["finding_id"]

    def test_dispatch_by_shape(self):
        svc = DarktraceIngestionService()
        assert svc.transform_alert_to_finding(MODEL_BREACH_SAMPLE)["description"]
        assert svc.transform_alert_to_finding(AI_ANALYST_SAMPLE)["description"]
        assert svc.transform_alert_to_finding(SYSTEM_STATUS_SAMPLE)["description"]


# ---------------------------------------------------------------------------
# HMAC / route tests
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_missing_signature_rejected(self, client):
        body = json.dumps(MODEL_BREACH_SAMPLE).encode()
        r = client.post(
            "/api/webhooks/darktrace/model-breach",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401

    def test_bad_signature_rejected(self, client):
        r = _post(
            client,
            "/api/webhooks/darktrace/model-breach",
            MODEL_BREACH_SAMPLE,
            sig="deadbeef" * 8,
        )
        assert r.status_code == 401

    def test_valid_signature_with_sha256_prefix(self, client):
        body = json.dumps(MODEL_BREACH_SAMPLE).encode()
        sig = "sha256=" + _sign(body)
        with patch("darktrace_webhook_under_test.DarktraceIngestionService") as MockSvc:
            instance = MockSvc.return_value
            instance.transform_model_breach.return_value = {
                "finding_id": "f-20260101-deadbeef"
            }
            instance.ingestion_service.ingest_finding.return_value = True
            r = client.post(
                "/api/webhooks/darktrace/model-breach",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Darktrace-Signature": sig,
                },
            )
        assert r.status_code == 202


class TestRoutes:
    def _patched_service(self, finding_id="f-20260101-deadbeef"):
        p = patch("darktrace_webhook_under_test.DarktraceIngestionService")
        MockSvc = p.start()
        instance = MockSvc.return_value
        instance.transform_model_breach.return_value = {"finding_id": finding_id}
        instance.transform_ai_analyst.return_value = {"finding_id": finding_id}
        instance.transform_system_status.return_value = {"finding_id": finding_id}
        instance.ingestion_service.ingest_finding.return_value = True
        return p, instance

    def test_model_breach_accepted(self, client):
        p, inst = self._patched_service()
        try:
            r = _post(
                client, "/api/webhooks/darktrace/model-breach", MODEL_BREACH_SAMPLE
            )
        finally:
            p.stop()
        assert r.status_code == 202
        assert r.json()["accepted"] is True
        inst.ingestion_service.ingest_finding.assert_called_once()

    def test_ai_analyst_accepted(self, client):
        p, inst = self._patched_service()
        try:
            r = _post(client, "/api/webhooks/darktrace/ai-analyst", AI_ANALYST_SAMPLE)
        finally:
            p.stop()
        assert r.status_code == 202
        inst.ingestion_service.ingest_finding.assert_called_once()

    def test_system_status_accepted(self, client):
        p, inst = self._patched_service()
        try:
            r = _post(
                client,
                "/api/webhooks/darktrace/system-status",
                SYSTEM_STATUS_SAMPLE,
            )
        finally:
            p.stop()
        assert r.status_code == 202

    def test_malformed_json_422(self, client):
        body = b"{not valid json"
        sig = _sign(body)
        r = client.post(
            "/api/webhooks/darktrace/model-breach",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Darktrace-Signature": sig,
            },
        )
        assert r.status_code == 422

    def test_untransformable_payload_422(self, client):
        # Missing pbid makes transform_model_breach return None
        r = _post(client, "/api/webhooks/darktrace/model-breach", {"score": 0.5})
        assert r.status_code == 422

    def test_health_endpoint(self, client):
        r = client.get("/api/webhooks/darktrace/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["secret_configured"] is True


class TestMisconfiguration:
    def test_no_secret_returns_503(self, monkeypatch):
        monkeypatch.delenv("DARKTRACE_WEBHOOK_SECRET", raising=False)
        app = FastAPI()
        app.include_router(darktrace_router, prefix="/api/webhooks/darktrace")
        c = TestClient(app)
        r = c.post(
            "/api/webhooks/darktrace/model-breach",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Darktrace-Signature": "x",
            },
        )
        assert r.status_code == 503
