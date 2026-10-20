"""The Redis the gateway and the llm-worker dial, read off ``REDIS_URL``.

Both go through ``redis_settings()`` -- the worker imports it -- so whatever it
drops is dropped for the queue and for the process that drains it. It read
host/port/db/password off the URL by hand, which lost the scheme and left the
password percent-encoded: a managed Redis (Azure Cache, ElastiCache) was dialled
in the clear, where it accepts the socket and answers nothing, and a key
containing ``@`` or ``/`` authenticated with its escapes still in it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.llm.gateway import gateway

pytestmark = pytest.mark.unit


def settings_for(monkeypatch, url: str):
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(redis_url=url))
    return gateway.redis_settings()


def test_rediss_asks_for_tls(monkeypatch):
    settings = settings_for(
        monkeypatch, "rediss://:s3cret@example.redis.cache.windows.net:6380/0"
    )
    assert settings.ssl is True
    assert settings.host == "example.redis.cache.windows.net"
    assert settings.port == 6380


def test_plain_redis_stays_unencrypted(monkeypatch):
    settings = settings_for(monkeypatch, "redis://localhost:6379/0")
    assert settings.ssl is False


def test_a_percent_encoded_password_is_decoded(monkeypatch):
    settings = settings_for(
        monkeypatch,
        "rediss://:p%2Fss%3Aword%40here@example.redis.cache.windows.net:6380/0",
    )
    assert settings.password == "p/ss:word@here"


def test_the_username_survives(monkeypatch):
    settings = settings_for(
        monkeypatch, "rediss://vigil:key@example.redis.cache.windows.net:6380/0"
    )
    assert settings.username == "vigil"


def test_the_slow_start_retries_are_kept(monkeypatch):
    settings = settings_for(monkeypatch, "redis://localhost:6379/0")
    assert (
        settings.conn_timeout,
        settings.conn_retries,
        settings.conn_retry_delay,
    ) == (
        3,
        15,
        3,
    )


def test_the_database_comes_from_the_path_or_the_query(monkeypatch):
    assert settings_for(monkeypatch, "redis://localhost:6379/4").database == 4
    assert settings_for(monkeypatch, "redis://localhost:6379?db=5").database == 5
