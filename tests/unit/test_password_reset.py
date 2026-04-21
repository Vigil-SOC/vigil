"""
Unit tests for backend.services.password_reset.

Redis-backed single-use enforcement is mocked so tests run without a live
cache. The signature + expiry logic is pure itsdangerous and runs as-is.
"""

import os

os.environ.setdefault("DEV_MODE", "true")

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.services import password_reset


@pytest.fixture(autouse=True)
def _mock_redis(monkeypatch):
    """Default every test to a mock Redis that records SET NX as success.
    Individual tests override as needed."""
    client = AsyncMock()
    client.set = AsyncMock(return_value=True)
    monkeypatch.setattr(password_reset, "_redis_client", lambda: client)
    return client


class TestPasswordReset:
    def test_token_roundtrip_returns_user_id(self):
        token = password_reset.generate_reset_token("user-abc")
        user_id = asyncio.run(password_reset.verify_reset_token(token))
        assert user_id == "user-abc"

    def test_tampered_token_is_rejected(self):
        token = password_reset.generate_reset_token("user-abc")
        tampered = token[:-2] + ("A" if token[-1] != "A" else "B") + "="
        user_id = asyncio.run(password_reset.verify_reset_token(tampered))
        assert user_id is None

    def test_token_for_one_user_does_not_unlock_another(self):
        token_a = password_reset.generate_reset_token("user-a")
        token_b = password_reset.generate_reset_token("user-b")
        a = asyncio.run(password_reset.verify_reset_token(token_a))
        b = asyncio.run(password_reset.verify_reset_token(token_b))
        assert a == "user-a"
        assert b == "user-b"

    def test_replay_is_rejected_when_redis_reports_used(self, _mock_redis):
        # First use succeeds (SET NX returns True), second attempt has the
        # key already present → SET NX returns False and verify rejects.
        _mock_redis.set = AsyncMock(side_effect=[True, False])
        token = password_reset.generate_reset_token("user-abc")
        first = asyncio.run(password_reset.verify_reset_token(token))
        second = asyncio.run(password_reset.verify_reset_token(token))
        assert first == "user-abc"
        assert second is None

    def test_redis_outage_is_fail_open(self, monkeypatch):
        # If Redis raises, we lose single-use enforcement but still honor
        # the signed TTL — verify the token is accepted.
        client = AsyncMock()
        client.set = AsyncMock(side_effect=RuntimeError("redis down"))
        monkeypatch.setattr(password_reset, "_redis_client", lambda: client)
        token = password_reset.generate_reset_token("user-abc")
        user_id = asyncio.run(password_reset.verify_reset_token(token))
        assert user_id == "user-abc"

    def test_expired_token_is_rejected(self, monkeypatch):
        # Force TTL to 0 so the token is considered expired the moment
        # itsdangerous parses it.
        monkeypatch.setattr(password_reset, "_ttl_seconds", lambda: 0)
        token = password_reset.generate_reset_token("user-abc")
        user_id = asyncio.run(password_reset.verify_reset_token(token))
        assert user_id is None
