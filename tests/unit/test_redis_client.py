"""REDIS_PASSWORD reaches every Redis URL, encoded, in place of the URL's own."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from core.config import get_settings
from core.redis_client import effective_redis_url

pytestmark = pytest.mark.unit

PW = "p@ss/w:rd#1"


def _env(monkeypatch, url=None, password=None):
    for name, value in (("REDIS_URL", url), ("REDIS_PASSWORD", password)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _password(url: str) -> str:
    return unquote(urlsplit(url).password)


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost:6379/0",
        "redis://:old@localhost:6379/0",
        "redis://:p%2Fold@localhost:6379/0",
        # Helm with Bitnami auth: the raw password substituted into the URL.
        f"redis://:{PW}@vigil-redis-master:6379/0",
    ],
)
def test_password_is_encoded_and_wins(monkeypatch, url):
    _env(monkeypatch, url, PW)
    out = effective_redis_url()
    parts = urlsplit(out)
    assert _password(out) == PW
    assert parts.path == "/0"
    assert parts.hostname in ("localhost", "vigil-redis-master")
    assert parts.port == 6379


def test_username_scheme_and_query_survive(monkeypatch):
    _env(monkeypatch, "rediss://vigil:old@cache.example:6380/2?ssl_cert_reqs=none", PW)
    out = effective_redis_url()
    assert out.startswith("rediss://vigil:")
    assert out.endswith("@cache.example:6380/2?ssl_cert_reqs=none")
    assert _password(out) == PW


def test_without_password_the_url_is_untouched(monkeypatch):
    _env(monkeypatch, "redis://:p%2Fold@localhost:6379/3")
    assert effective_redis_url() == "redis://:p%2Fold@localhost:6379/3"
    _env(monkeypatch, "redis://localhost:6379/3", "")
    assert effective_redis_url() == "redis://localhost:6379/3"


def test_a_password_alone_configures_no_redis(monkeypatch):
    _env(monkeypatch, password=PW)
    assert effective_redis_url(default=None) is None
    assert _password(effective_redis_url()) == PW  # localhost default


def test_password_not_in_settings_repr(monkeypatch):
    _env(monkeypatch, "redis://localhost:6379/0", PW)
    assert PW not in repr(get_settings())
