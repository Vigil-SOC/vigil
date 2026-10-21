"""An install that leaves VIGIL_CSRF_REPORT_ONLY unset enforces CSRF (#2576).

tests/conftest.py pins report-only for the whole suite, and services/api/main.py
builds its middleware at import, so this test unsets the pin and builds its own
app to see the shipped default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

HELM_VALUES = Path(__file__).resolve().parents[3] / "infra/helm/vigil/values.yaml"


def test_unset_report_only_rejects_a_post_without_the_header(monkeypatch):
    from core.config import get_settings
    from services.api.middleware.csrf import CSRFMiddleware

    monkeypatch.delenv("VIGIL_CSRF_REPORT_ONLY", raising=False)
    get_settings.cache_clear()
    try:
        app = FastAPI()
        app.add_middleware(CSRFMiddleware)

        @app.post("/api/cases")
        def create():
            return {"ok": True}

        client = TestClient(app)
        client.cookies.set("csrf_token", "abc")
        assert client.post("/api/cases").status_code == 403
        ok = client.post("/api/cases", headers={"X-CSRF-Token": "abc"})
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()


def test_helm_values_leave_report_only_to_the_code_default():
    assert "VIGIL_CSRF_REPORT_ONLY" not in HELM_VALUES.read_text(encoding="utf-8")
