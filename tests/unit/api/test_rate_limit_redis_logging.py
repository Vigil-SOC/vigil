"""The rate limiter must log at ERROR when configured Redis is unusable."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from core import redis_client
from services.api.middleware import rate_limit


class _FakeLimiter:
    def __init__(self, healthy: bool, **kwargs):
        self.limiter = SimpleNamespace(storage=SimpleNamespace(check=lambda: healthy))


def _build(monkeypatch, url, healthy):
    monkeypatch.setattr(
        redis_client,
        "get_settings",
        lambda: SimpleNamespace(redis_url=url, redis_password=None),
    )
    monkeypatch.setattr(rate_limit, "Limiter", lambda **kw: _FakeLimiter(healthy, **kw))
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


def test_password_alone_stays_in_memory(monkeypatch, caplog):
    monkeypatch.setattr(
        redis_client,
        "get_settings",
        lambda: SimpleNamespace(redis_url=None, redis_password="secret"),
    )
    with caplog.at_level(logging.INFO, logger=rate_limit.__name__):
        built = rate_limit._build_limiter()

    assert not caplog.records
    assert built._storage_uri is None


def test_limiter_receives_the_encoded_override(monkeypatch):
    password = "p@ss/w:rd#1"
    monkeypatch.setattr(
        redis_client,
        "get_settings",
        lambda: SimpleNamespace(
            redis_url=f"redis://:{password}@redis-master:6379/0",
            redis_password=password,
        ),
    )
    seen: dict = {}

    class _Recording:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.limiter = SimpleNamespace(storage=SimpleNamespace(check=lambda: True))

    monkeypatch.setattr(rate_limit, "Limiter", _Recording)
    assert rate_limit._build_limiter() is not None
    assert password not in seen["storage_uri"]
    assert seen["storage_uri"].endswith("@redis-master:6379/0")
