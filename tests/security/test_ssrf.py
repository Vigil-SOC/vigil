"""Regression tests for FIN-005 — SSRF via LLM provider discovery.

Covers the ``services.url_safety.validate_provider_url`` gate that all
provider-discovery / provider-test paths now run through.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

from services.url_safety import (  # noqa: E402
    DEFAULT_ALLOWED_PROVIDER_HOSTS,
    UrlSafetyError,
    validate_provider_url,
)

pytestmark = pytest.mark.unit


# Every entry must raise UrlSafetyError. Sourced from the disclosure
# plus the standard SSRF playbook.
BLOCKED_URLS = [
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://[::1]:11434",
    "http://0.0.0.0/admin",
    "http://169.254.169.254/latest/meta-data?x=",
    "http://10.0.0.1/admin?x=",
    "http://172.16.0.1/admin?x=",
    "http://192.168.1.1/admin?x=",
    "http://fd00::1/admin",  # private IPv6
    "file:///etc/passwd",
    "gopher://attacker/",
    "ftp://attacker/",
    "http://user:pass@api.openai.com/v1",  # userinfo banned
    "http://api.openai.com/v1#frag",  # fragment banned
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_blocked_url_rejected(url):
    with pytest.raises(UrlSafetyError):
        validate_provider_url(url, allow_custom=True)


def test_allowlisted_host_passes():
    safe = validate_provider_url("https://api.openai.com/v1", allow_custom=False)
    assert safe.is_allowlisted_host
    assert safe.sanitized == "https://api.openai.com/v1"


def test_query_string_stripped():
    """The disclosure showed ``base_url=http://x/foo?proof=`` letting an
    attacker control the path of the final request once the handler
    appended ``/models``. Verify the validator strips the query
    string."""
    safe = validate_provider_url(
        "https://api.openai.com/internal/status?proof=", allow_custom=False
    )
    assert "?" not in safe.sanitized
    assert "proof" not in safe.sanitized
    assert safe.sanitized == "https://api.openai.com/internal/status"


def test_non_allowlisted_requires_allow_custom():
    with pytest.raises(UrlSafetyError):
        validate_provider_url("https://attacker.example/", allow_custom=False)


def test_default_allowed_hosts_includes_openai_anthropic():
    assert "api.openai.com" in DEFAULT_ALLOWED_PROVIDER_HOSTS
    assert "api.anthropic.com" in DEFAULT_ALLOWED_PROVIDER_HOSTS
