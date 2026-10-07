"""Credentials a service names in its logs: the password-reset token and Redis URLs."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.ingestion.dedup import RedisDedupSet
from core.platform import email_service
from core.platform.log_redaction import mask_email, redact_url
from core.routing import request_unit_of_work
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

EMAIL = "victim@example.com"
PASSWORD = "s3cr3t-redis-pw"


class _Session:
    def __init__(self, user):
        self._user = user

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return self._user


def _client(user) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[request_unit_of_work] = lambda: _Session(user)
    return TestClient(app)


def _settings(monkeypatch, *, dev_mode: bool):
    real = email_service.get_settings()
    patched = SimpleNamespace(
        dev_mode=dev_mode, smtp_from=real.smtp_from, vigil_frontend_url=""
    )
    monkeypatch.setattr(email_service, "get_settings", lambda: patched)
    monkeypatch.setattr(auth, "get_settings", lambda: patched)


@pytest.fixture(autouse=True)
def _console_backend(monkeypatch):
    monkeypatch.setattr(email_service, "_backend", email_service.ConsoleBackend())
    limiter.reset()


def _request_reset(monkeypatch, user, caplog):
    caplog.set_level(logging.DEBUG)
    response = _client(user).post(
        "/api/auth/password-reset/request", json={"email": EMAIL}
    )
    assert response.status_code == 200
    return "\n".join(r.getMessage() for r in caplog.records)


class TestPasswordResetLogging:
    def test_console_backend_logs_neither_token_nor_address(self, monkeypatch, caplog):
        _settings(monkeypatch, dev_mode=False)
        user = SimpleNamespace(
            user_id="u1", username="victim", full_name="V", email=EMAIL, is_active=True
        )
        sent = []
        real_send = email_service.ConsoleBackend.send
        monkeypatch.setattr(
            email_service.ConsoleBackend,
            "send",
            lambda self, **kw: (sent.append(kw["body"]), real_send(self, **kw))[1],
        )
        logged = _request_reset(monkeypatch, user, caplog)

        token = sent[0].split("(token) ")[1].split()[0]
        assert token and token not in logged
        assert EMAIL not in logged
        assert "[email/console] not sent" in logged

    def test_dev_mode_still_shows_the_message(self, monkeypatch, caplog):
        _settings(monkeypatch, dev_mode=True)
        user = SimpleNamespace(
            user_id="u1", username="victim", full_name="V", email=EMAIL, is_active=True
        )
        assert "(token) " in _request_reset(monkeypatch, user, caplog)

    def test_unknown_address_is_not_logged(self, monkeypatch, caplog):
        _settings(monkeypatch, dev_mode=False)
        assert EMAIL not in _request_reset(monkeypatch, None, caplog)

    def test_send_failure_masks_the_recipient(self, monkeypatch, caplog):
        class Boom(email_service.EmailBackend):
            def send(self, **_):
                raise RuntimeError("relay down")

        monkeypatch.setattr(email_service, "_backend", Boom())
        email_service.send_email(to=EMAIL, subject="s", body="b")
        assert EMAIL not in caplog.text
        assert mask_email(EMAIL) in caplog.text


class TestRedisUrlLogging:
    def test_redact_url_drops_userinfo_and_query(self):
        assert (
            redact_url(f"redis://:{PASSWORD}@redis-master:6379/0")
            == "redis://redis-master:6379/0"
        )
        assert redact_url(f"rediss://u:{PASSWORD}@[::1]:6380/2?x={PASSWORD}") == (
            "rediss://[::1]:6380/2"
        )
        assert redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
        assert redact_url(PASSWORD) == "<unparseable url>"

    def test_dedup_connect_line_has_no_password(self, monkeypatch, caplog):
        class _Redis:
            async def ping(self):
                return True

        monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: _Redis())
        dedup = RedisDedupSet(
            "unit", redis_url=f"redis://:{PASSWORD}@redis-master:6379/0"
        )
        caplog.set_level(logging.DEBUG)

        assert asyncio.run(dedup._get_redis()) is not None
        assert "redis-master:6379" in caplog.text
        assert PASSWORD not in caplog.text
