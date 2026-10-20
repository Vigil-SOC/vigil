"""Shape and form of a provider ``base_url``, shared by every writer of one.

Two writers set a host on a provider: the Vigil provider API (``llm_provider_configs``)
and the Bifrost config proxy (the provider document's ``network_config``). They
must agree on what a URL is, so the rules live here once.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def validate_base_url_shape(base_url: Optional[str], subject: str = "base_url") -> None:
    """Cheap shape-only validation for a stored ``base_url``; raises ``ValueError``.

    Admins legitimately persist loopback URLs for self-hosted Ollama or
    private LLM gateways, so the SSRF IP gate doesn't apply here — it
    runs at HTTP-request time inside the discovery/test helpers. We
    still reject malformed inputs and obvious smuggling vectors (non-
    http(s) scheme, userinfo, fragment, a port the gateway can't dial)
    so they never reach disk. ``subject`` opens each message, naming the
    field to whoever reads the 400.
    """
    if base_url is None or not base_url.strip():
        return
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{subject} must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError(f"{subject} has no host")
    if parsed.username or parsed.password:
        raise ValueError(f"{subject} must not include userinfo")
    if parsed.fragment:
        raise ValueError(f"{subject} must not include a fragment")
    try:
        port = parsed.port  # raises on "11434." and anything out of 0-65535
    except ValueError:
        port = 0
    if port == 0:  # urllib takes ":0" as a port; nothing listens there
        raw = parsed.netloc.rpartition("@")[2].rpartition(":")[2]
        raise ValueError(f'{subject} has an invalid port "{raw}"')


def normalize_openai_compat_base_url(base_url: str) -> str:
    """Strip a trailing ``/v1``; Bifrost appends the versioned path itself.

    Any other path prefix (``/api``, ``/openai``, a tenant slug) is kept.
    """
    url = base_url.strip().rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url
