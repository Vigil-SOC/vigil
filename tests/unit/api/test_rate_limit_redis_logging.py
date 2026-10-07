"""The rate limiter must log at ERROR when configured Redis is unusable."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from services.api.middleware import rate_limit


class _FakeLimiter:
    def __init__(self, healthy: bool, **kwargs):
        self.limiter = SimpleNamespace(storage=SimpleNamespace(check=lambda: healthy))


def _build(monkeypatch, url, healthy):
    monkeypatch.setattr(
        rate_limit, "get_settings", lambda: SimpleNamespace(redis_url=url)
    )
    monkeypatch.setattr(
        rate_limit, "Limiter", lambda **kw: _FakeLimiter(healthy, **kw)
    )
    return rate_limit._build_limiter()


def test_unusable_redis_logs_error(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=rate_limit.__name__):
        _build(monkeypatch, "redis://unreachable:6379/0", healthy=False)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "until the process restarts" in errors[0].getMessage()


def test_no_redis_configured_is_quiet(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=rate_limit.__name__):
        _build(monkeypatch, "", healthy=False)

    assert not caplog.records
