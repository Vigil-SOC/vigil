"""Revocation keys live only in Redis, so an evicting Redis has to be called out."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from core.auth import token_blacklist as tb


class _Redis:
    def __init__(self, policy):
        self.policy = policy
        self.sets = []

    async def config_get(self, name):
        if isinstance(self.policy, Exception):
            raise self.policy
        return {name: self.policy}

    async def set(self, key, value, ex=None, nx=False):
        self.sets.append((key, ex))


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["allkeys-lru", "volatile-lru", "allkeys-random"])
async def test_evicting_policy_is_an_error(monkeypatch, caplog, policy):
    monkeypatch.setattr(tb, "_get_client", lambda: _Redis(policy))
    await tb.warn_if_redis_evicts()
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["noeviction", PermissionError("blocked")])
async def test_noeviction_or_blocked_config_is_silent(monkeypatch, caplog, policy):
    monkeypatch.setattr(tb, "_get_client", lambda: _Redis(policy))
    await tb.warn_if_redis_evicts()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_user_cutoff_expires_with_the_longest_token(monkeypatch):
    client = _Redis("noeviction")
    monkeypatch.setattr(tb, "_get_client", lambda: client)
    monkeypatch.setattr(tb.sign_ins, "end_all_for_user", lambda *a: 0)
    await tb.revoke_all_for_user("u-1")
    assert client.sets == [
        (
            "user_revoked_before:u-1",
            tb.get_settings().jwt_refresh_expiration_days * 86400,
        )
    ]


@pytest.mark.asyncio
async def test_missing_redis_does_not_pretend_a_token_was_revoked(monkeypatch):
    monkeypatch.setattr(tb, "_get_client", lambda: None)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(tb.RevocationWriteError):
        await tb.blacklist_jti("jti-1", future)
    with pytest.raises(tb.RevocationWriteError):
        await tb.revoke_all_for_user("u-1")


@pytest.mark.asyncio
async def test_an_expired_token_needs_no_redis(monkeypatch):
    monkeypatch.setattr(tb, "_get_client", lambda: None)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await tb.blacklist_jti("jti-1", past)
