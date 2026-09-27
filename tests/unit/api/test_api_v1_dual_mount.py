"""The dual-mount mechanism (`RouterMeta.legacy_prefixes`) actually works.

The entire /api/v1 port rests on one behaviour: a contract router is served at
both its versioned path and its pre-version path, by the *same* handler. The
shadow-guard test only compares prefix strings; this exercises the real routing
so the frozen paths (31 paths, 38 ops) are not resting on behaviour nothing runs.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
os.environ.setdefault("DEV_MODE", "true")

pytestmark = pytest.mark.unit


def test_every_v1_path_is_also_served_at_its_legacy_prefix():
    """Each frozen /api/v1/<res>/... path is also served at /api/<res>/... —
    the legacy alias the console keeps calling. Read from the live OpenAPI, so
    it reflects what is actually mounted, not a string concatenation."""
    from services.api.main import app

    # Normalize trailing slashes: a collection route may be served with a
    # trailing slash on one prefix and without on the other (the workflows
    # catalog is the delegate case); both resolve to the same handler via a 307,
    # so "/api/workflows" and "/api/workflows/" are the same alias.
    def norm(p: str) -> str:
        return p.rstrip("/") or "/"

    served = {norm(p) for p in app.openapi()["paths"]}
    v1_paths = [p for p in app.openapi()["paths"] if p.startswith("/api/v1/")]
    assert v1_paths, "no /api/v1 paths served — mounting is broken"

    missing = []
    for p in v1_paths:
        legacy = norm("/api/" + p[len("/api/v1/"):])
        if legacy not in served:
            missing.append(f"{p} has no legacy alias {legacy}")
    assert not missing, "dual-mount incomplete:\n" + "\n".join(missing)


def test_both_paths_return_the_same_response():
    """Functional: hit both addresses through the app and get identical output.
    Auth and the data layer are overridden so this needs no DB."""
    from services.api.main import app
    from services.api.middleware.auth import get_current_active_user
    import core.api.v1.findings_router as v1_findings

    class _User:
        is_active = True
        username = "test"
        user_id = "test"

    app.dependency_overrides[get_current_active_user] = lambda: _User()
    orig_count = v1_findings.data_service.count_findings
    orig_list = v1_findings.data_service.get_findings
    v1_findings.data_service.count_findings = lambda **_: 0
    v1_findings.data_service.get_findings = lambda **_: []
    try:
        client = TestClient(app)
        v1 = client.get("/api/v1/findings/")
        legacy = client.get("/api/findings/")
        assert v1.status_code == 200, v1.text
        assert legacy.status_code == 200, legacy.text
        assert v1.json() == legacy.json()
        assert v1.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        v1_findings.data_service.count_findings = orig_count
        v1_findings.data_service.get_findings = orig_list
