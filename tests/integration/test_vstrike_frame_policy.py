from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from core.integrations.vstrike.frame_origin import configured_frame_origin
from services.api.middleware.security_headers import SecurityHeadersMiddleware


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://vstrike.example.test/path?ignored=true",
            "https://vstrike.example.test",
        ),
        ("http://127.0.0.1:9000", "http://127.0.0.1:9000"),
        ("https://[2001:db8::1]:443/path", "https://[2001:db8::1]:443"),
        ("https://user:password@example.test", None),
        ("javascript:alert(1)", None),
        ("https://example.test;script-src%20*", None),
        ("https://example.test:invalid", None),
    ],
)
def test_frame_origin_is_a_validated_origin_only(url, expected):
    with patch(
        "core.integrations.vstrike.frame_origin.get_integration_config",
        return_value={"url": url},
    ), patch("core.integrations.vstrike.frame_origin.get_secret", return_value=None):
        assert configured_frame_origin() == expected


def test_default_document_policy_admits_only_configured_frame_origin():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.get("/")(lambda: HTMLResponse("<p>Vigil</p>"))
    app.get("/api/check")(lambda: {"ok": True})
    with patch(
        "core.integrations.vstrike.frame_origin.configured_frame_origin",
        return_value="https://vstrike.example.test",
    ):
        client = TestClient(app)
        policy = client.get("/").headers["content-security-policy"]
        assert "frame-src 'self' https://vstrike.example.test" in policy
        assert "script-src 'self';" in policy
        assert "frame-ancestors 'none'" in policy
        assert (
            "vstrike.example.test"
            not in client.get("/api/check").headers["content-security-policy"]
        )


def test_custom_policy_remains_authoritative():
    app = FastAPI()
    policy = "default-src 'self'; frame-src 'none'"
    app.add_middleware(SecurityHeadersMiddleware, csp_policy=policy)
    app.get("/")(lambda: HTMLResponse("<p>Vigil</p>"))
    with patch(
        "core.integrations.vstrike.frame_origin.configured_frame_origin"
    ) as resolve:
        assert TestClient(app).get("/").headers["content-security-policy"] == policy
    resolve.assert_not_called()
