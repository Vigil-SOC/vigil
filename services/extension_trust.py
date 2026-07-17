"""Single source of truth for which page-extension connector origins Vigil trusts.

One operator-set allowlist (``EXTENSION_CONNECTOR_ALLOWLIST``) is read by all
three consumers so they can't drift: the SSRF guard (mints session tokens by
calling the connector), the CSP (admits the origin into script-src/connect-src),
and the frontend trust gate. Trusting an origin here runs its code in Vigil's own
browser origin, so it's a deliberate operator control — separate from the
app-admin act of configuring a connector URL in Settings.
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

_ALLOWLIST_ENV = "EXTENSION_CONNECTOR_ALLOWLIST"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def canonical_origin(value: str) -> Optional[str]:
    """Normalize a URL/origin to ``scheme://host[:port]``, or ``None`` if it has
    no scheme+host — so a junk entry is dropped, not turned into a wildcard."""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.hostname:
        return None
    origin = f"{parts.scheme.lower()}://{parts.hostname.lower()}"
    if parts.port:
        origin = f"{origin}:{parts.port}"
    return origin


def connector_allowlist_origins() -> list[str]:
    """Canonicalized, de-duplicated trusted connector origins from
    ``EXTENSION_CONNECTOR_ALLOWLIST`` (may be empty)."""
    raw = os.getenv(_ALLOWLIST_ENV, "")
    seen: set[str] = set()
    origins: list[str] = []
    for entry in raw.split(","):
        origin = canonical_origin(entry)
        if origin and origin not in seen:
            seen.add(origin)
            origins.append(origin)
    return origins


def is_trusted_connector_url(url: str) -> bool:
    """Require https (http only for loopback) and, when an allowlist is set,
    membership. Empty allowlist → scheme rule only (the CSP still blocks a
    non-loopback origin that isn't allowlisted)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    loopback = host in _LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        return False
    allow = connector_allowlist_origins()
    if not allow:
        return True
    origin = canonical_origin(url)
    return origin is not None and origin in allow
