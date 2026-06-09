"""End-to-end check that the unauthenticated endpoints called out in
the 2026-05 disclosure now return 401 instead of 200.

Runs against the real FastAPI app from ``backend.main`` with
``DEV_MODE=false`` forced (otherwise the auth middleware would short-
circuit to a mock admin user and the test would silently no-op).

Imports are at module scope so collection happens before any
``test_monitoring``-style test can clobber ``sys.modules['fastapi']``
with mock objects — that pre-existing isolation bug would otherwise
make this file unrunnable in the full suite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

# Pre-import at collection time so ``sys.modules`` is locked in before
# anything else runs.
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
from fastapi.testclient import TestClient  # noqa: E402

from backend import main as backend_main  # noqa: E402
from backend.middleware import auth as auth_module  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def app():
    """Build a TestClient with auth force-enabled regardless of DEV_MODE.

    Patches the module-level ``DEV_MODE`` on the already-imported
    ``backend.middleware.auth`` so ``get_current_user`` takes the real
    validation path. Restores the previous value on teardown.
    """
    prev = auth_module.DEV_MODE
    auth_module.DEV_MODE = False
    try:
        yield TestClient(backend_main.app)
    finally:
        auth_module.DEV_MODE = prev


# Each tuple: HTTP method, URL path, optional JSON body.
# These paths were called out as unauthenticated in the disclosure.
PROTECTED_ROUTES = [
    ("GET", "/api/findings/", None),
    ("DELETE", "/api/findings/all", None),
    ("GET", "/api/config/secrets/status", None),
    ("GET", "/api/llm/providers/", None),
    (
        "POST",
        "/api/llm/providers/discover-models",
        {"provider_type": "ollama", "base_url": "http://127.0.0.1:11434"},
    ),
    ("GET", "/api/custom-integrations/list", None),
    (
        "POST",
        "/api/custom-integrations/save",
        {
            "integration_id": "evil",
            "metadata": {},
            "server_code": "print('x')",
        },
    ),
    ("GET", "/api/mcp/servers/enabled", None),
    ("PUT", "/api/mcp/servers/mempalace/enabled", {"enabled": False}),
    ("GET", "/api/orchestrator/status", None),
    ("POST", "/api/orchestrator/investigations/purge", None),
    ("GET", "/api/approvals/pending", None),
    (
        "POST",
        "/api/integrations/compatibility/install",
        {"integration_id": "misp"},
    ),
    ("GET", "/api/claude/sdk-status", None),
    ("GET", "/api/claude/models", None),
    (
        "POST",
        "/api/claude/chat",
        {
            "messages": [{"role": "user", "content": "auth gate test"}],
            "max_tokens": 1,
            "enable_thinking": False,
            "streaming": False,
            "use_agent_sdk": False,
        },
    ),
    (
        "POST",
        "/api/claude/chat/stream",
        {
            "messages": [{"role": "user", "content": "auth gate test"}],
            "max_tokens": 1,
        },
    ),
    (
        "POST",
        "/api/claude/agent/task",
        {"task": "auth gate test only", "max_turns": 1, "allowed_tools": []},
    ),
    ("POST", "/api/claude/analyze-finding?finding_id=auth-gate-test", None),
    ("GET", "/api/webhooks/", None),
    (
        "POST",
        "/api/webhooks/",
        {"name": "poc", "url": "https://example.com", "events": ["case_created"]},
    ),
    ("PUT", "/api/webhooks/webhook-001", {"name": "poc-updated"}),
    ("DELETE", "/api/webhooks/webhook-001", None),
    ("POST", "/api/webhooks/webhook-001/test", None),
    ("GET", "/api/webhooks/webhook-001/deliveries", None),
    ("GET", "/api/integrations/vstrike/health", None),
    ("GET", "/api/integrations/vstrike/ui/networks", None),
    ("POST", "/api/integrations/vstrike/ui/iframe-token", None),
    ("GET", "/api/integrations/vstrike/topology/asset/test-asset", None),
    # Routes that were on bare `router` (no auth) — fixed in issue #286.
    ("POST", "/api/integrations/vstrike/network-graph", {"network_id": "test"}),
    ("POST", "/api/integrations/vstrike/ui/legend-apply", {"legend_run_id": "test"}),
    ("POST", "/api/integrations/vstrike/ui/rightpanel-focus", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
def test_unauthenticated_request_is_rejected(app, method, path, body):
    """No Authorization header, no cookie — must get 401 (or 403).

    The pre-fix behavior was 200 (or in some cases 500 because the
    handler reached the dangerous backend call before any error). Both
    are wrong: a missing-auth deny must happen at the dependency layer
    before the handler runs.
    """
    response = app.request(method, path, json=body)
    # Some routers return 403 (e.g. inactive user path), some 401.
    # The disclosure's expectation is "not 2xx and not the
    # operation-failure 500" — accept either auth code.
    assert response.status_code in (401, 403), (
        f"{method} {path} returned {response.status_code} "
        f"(body: {response.text[:200]})"
    )


def test_unauthenticated_claude_upload_file_is_rejected(app):
    """The Claude file upload helper must not read files before auth."""
    response = app.post(
        "/api/claude/upload-file",
        files={
            "file": (
                "auth-test.txt",
                b"synthetic auth gate test\n",
                "text/plain",
            )
        },
    )

    assert response.status_code in (401, 403), (
        f"POST /api/claude/upload-file returned {response.status_code} "
        f"(body: {response.text[:200]})"
    )


def test_vstrike_inbound_without_bearer_uses_api_key_gate(app):
    """VStrike /findings stays public from session auth but not open."""
    response = app.post(
        "/api/integrations/vstrike/findings",
        json={"batch_id": "auth-gate", "findings": []},
    )

    assert response.status_code in (401, 403, 503), (
        f"POST /api/integrations/vstrike/findings returned {response.status_code} "
        f"(body: {response.text[:200]})"
    )


# Routes that require admin permission on top of authentication.
# An authenticated non-admin must be rejected with 403.
ADMIN_ONLY_ROUTES = [
    (
        "POST",
        "/api/integrations/compatibility/install",
        {"integration_id": "misp"},
    ),
    (
        "POST",
        "/api/custom-integrations/save",
        {
            "integration_id": "smoke",
            "metadata": {},
            "server_code": "# noop",
        },
    ),
    ("GET", "/api/custom-integrations/list", None),
    ("PUT", "/api/mcp/servers/mempalace/enabled", {"enabled": False}),
    ("POST", "/api/mcp/servers/reload", None),
    (
        "POST",
        "/api/llm/providers/discover-models",
        {"provider_type": "ollama", "base_url": "http://127.0.0.1:11434"},
    ),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ONLY_ROUTES)
def test_authenticated_non_admin_is_rejected(method, path, body, monkeypatch):
    """Authenticated user without admin permission must get 403.

    Verifies the per-route ``AuthService.check_permission(..., 'integrations.write')``
    / ``settings.write`` gates the disclosure's fixes added on top of
    router-level auth.
    """
    from database.models import User

    fake_user = User(
        user_id="non-admin",
        username="non-admin",
        email="user@test.local",
        password_hash="",
        role_id="user-role",
        is_active=True,
        mfa_enabled=False,
    )

    async def _fake_get_user(*args, **kwargs):
        return fake_user

    # Pretend an authenticated user is making the call.
    backend_main.app.dependency_overrides[auth_module.get_current_active_user] = (
        lambda: fake_user
    )
    backend_main.app.dependency_overrides[auth_module.get_current_user] = (
        lambda: fake_user
    )
    # And that AuthService.check_permission returns False (no admin).
    monkeypatch.setattr(
        "backend.services.auth_service.AuthService.check_permission",
        lambda user_id, permission, session=None: False,
    )

    try:
        client = TestClient(backend_main.app)
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (
            f"{method} {path} returned {response.status_code} "
            f"(expected 403) (body: {response.text[:200]})"
        )
    finally:
        backend_main.app.dependency_overrides.pop(
            auth_module.get_current_active_user, None
        )
        backend_main.app.dependency_overrides.pop(auth_module.get_current_user, None)
