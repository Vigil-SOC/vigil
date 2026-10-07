"""Redis outage logging for token revocation lookups."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core.auth import token_blacklist as tb


class _FlakyRedis:
    def __init__(self):
        self.down = True

    async def exists(self, key):
        if self.down:
            raise ConnectionError("redis down")
        return 0

    async def get(self, key):
        return None


@pytest.fixture
def flaky(monkeypatch):
    client = _FlakyRedis()
    monkeypatch.setattr(tb, "_get_client", lambda: client)
    monkeypatch.setattr(tb, "_FAIL_OPEN", False)
    monkeypatch.setattr(tb, "_outage_failures", 0)
    monkeypatch.setattr(tb, "_outage_total", 0)
    monkeypatch.setattr(tb, "_outage_last_logged", 0.0)
    return client


@pytest.mark.asyncio
async def test_outage_logs_one_error_per_window_then_recovery(flaky, caplog):
    caplog.set_level(logging.INFO, logger=tb.__name__)
    payload = {"jti": "j", "user_id": "u"}

    for _ in range(5):
        assert await tb.is_token_revoked(payload) is True  # still fail-closed

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1

    flaky.down = False
    assert await tb.is_token_revoked(payload) is False
    assert await tb.is_token_revoked(payload) is False

    recovered = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(recovered) == 1
    assert "5 failure(s)" in recovered[0].getMessage()


@pytest.mark.asyncio
async def test_error_repeats_after_window_with_count(flaky, caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger=tb.__name__)
    clock = iter([100.0, 110.0, 170.0])
    monkeypatch.setattr(tb, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    for _ in range(3):
        await tb.is_token_revoked({"jti": "j"})

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "2 failure(s)" in messages[1]
