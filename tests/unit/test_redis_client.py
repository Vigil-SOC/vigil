"""REDIS_PASSWORD overrides the URL password, encoded, and stays out of repr."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

import pytest

from core.agents.queue import _redis_url
from core.config import get_settings
from core.redis_client import (
    arq_redis_settings,
    configured_redis_url,
    effective_redis_url,
    open_async_redis,
)

pytestmark = pytest.mark.unit

PASSWORD = "p@ss/w:rd#1"
ENCODED = quote(PASSWORD, safe="")


def _set(monkeypatch, url: str | None, password: str | None):
    if url is None:
        monkeypatch.delenv("REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("REDIS_URL", url)
    if password is None:
        monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("REDIS_PASSWORD", password)
    get_settings.cache_clear()


def test_password_is_encoded_and_overrides_the_url(monkeypatch):
    _set(monkeypatch, "rediss://vigil:old@example.cache:6380/4?db=5", PASSWORD)
    url = effective_redis_url()
    assert url == f"rediss://vigil:{ENCODED}@example.cache:6380/4?db=5"
    assert PASSWORD not in url
    parts = urlsplit(url)
    assert parts.username == "vigil"
    assert unquote(parts.password or "") == PASSWORD
    assert parts.hostname == "example.cache"
    assert parts.port == 6380
    assert parts.path == "/4"
    assert parts.query == "db=5"


def test_helm_raw_password_is_stripped_before_parsing(monkeypatch):
    # The kubelet has already substituted REDIS_PASSWORD into the URL, raw.
    # `/` and `#` must not become the path and the fragment.
    raw = f"redis://:{PASSWORD}@redis-master:6379/0"
    _set(monkeypatch, raw, PASSWORD)
    url = effective_redis_url()
    assert url == f"redis://:{ENCODED}@redis-master:6379/0"
    parts = urlsplit(url)
    assert parts.hostname == "redis-master"
    assert parts.path == "/0"
    assert parts.fragment == ""
    assert unquote(parts.password or "") == PASSWORD


def test_username_token_is_not_reencoded(monkeypatch):
    _set(monkeypatch, "redis://user+name:old@redis-master:6379/0", "pw")
    assert effective_redis_url() == "redis://user+name:pw@redis-master:6379/0"
    _set(monkeypatch, "redis://acme%2fops:old@redis-master:6379/1", "pw")
    assert effective_redis_url() == "redis://acme%2fops:pw@redis-master:6379/1"
    assert arq_redis_settings().username == "acme%2fops"


def test_helm_raw_password_keeps_the_username(monkeypatch):
    _set(monkeypatch, f"redis://vigil:{PASSWORD}@redis-master:6379/2", PASSWORD)
    url = effective_redis_url()
    assert url == f"redis://vigil:{ENCODED}@redis-master:6379/2"
    assert urlsplit(url).username == "vigil"


def test_without_a_password_the_url_is_unchanged(monkeypatch):
    original = "rediss://vigil:p%2Fss%3Aword%40here@example:6380/0"
    _set(monkeypatch, original, None)
    assert effective_redis_url() == original
    _set(monkeypatch, original, "")
    assert effective_redis_url() == original


def test_password_alone_does_not_invent_a_configured_url(monkeypatch):
    _set(monkeypatch, None, PASSWORD)
    assert configured_redis_url() is None
    url = effective_redis_url()
    assert url == f"redis://:{ENCODED}@localhost:6379/0"
    assert PASSWORD not in url


def test_arq_receives_the_decoded_override(monkeypatch):
    _set(monkeypatch, f"redis://vigil:{PASSWORD}@redis-master:6380/3", PASSWORD)
    settings = arq_redis_settings()
    assert settings.host == "redis-master"
    assert settings.port == 6380
    assert settings.database == 3
    assert settings.username == "vigil"
    assert settings.password == PASSWORD
    assert settings.ssl is False


def test_queue_takes_the_effective_url(monkeypatch):
    _set(monkeypatch, "redis://localhost:6379/0", PASSWORD)
    assert _redis_url() == f"redis://:{ENCODED}@localhost:6379/0"
    assert PASSWORD not in _redis_url()


def test_password_is_absent_from_repr(monkeypatch):
    _set(monkeypatch, "redis://localhost:6379/0", PASSWORD)
    settings = get_settings()
    client = open_async_redis()
    for value in (
        settings,
        settings.redis_password,
        client,
        repr(effective_redis_url()),
    ):
        assert PASSWORD not in repr(value)
