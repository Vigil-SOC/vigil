"""Centralized URL safety / SSRF protection.

Used by the LLM provider discovery and test paths so a user-supplied
``base_url`` can never coerce the backend into reaching out to
loopback, private, link-local, or cloud-metadata addresses.

Closes the SSRF leg of the 2026-05 security disclosure: previously
``POST /api/llm/providers/discover-models`` accepted any ``base_url``
and the backend would happily fetch it.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse, urlunparse

# Hosts we trust unconditionally — official public LLM provider APIs.
# Anything not in this set is treated as a custom URL and is only
# allowed when ``allow_custom=True`` AND none of its resolved IPs are
# in a blocked range.
DEFAULT_ALLOWED_PROVIDER_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
    }
)


class UrlSafetyError(ValueError):
    """Raised when a URL fails safety validation."""


@dataclass(frozen=True)
class SafeUrl:
    """A URL that has passed validation.

    ``sanitized`` is the URL with userinfo, query string, and fragment
    stripped — callers should use this rather than the original. The
    caller is still responsible for appending fixed paths (e.g.
    ``/models``) on top of ``sanitized``.

    ``is_allowlisted_host`` lets the caller decide whether to forward
    sensitive headers (e.g. a bearer token): only forward to known
    hosts so user-supplied credentials never leak to attacker-chosen
    destinations.
    """

    sanitized: str
    host: str
    port: int
    scheme: str
    is_allowlisted_host: bool


def _resolve_host(host: str) -> Iterable[str]:
    """Resolve a hostname (or IP literal) to one or more IPs.

    Returns an iterable of stringified addresses. Raises
    ``UrlSafetyError`` if the host cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UrlSafetyError(f"could not resolve host: {host}") from exc
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            yield addr


def _is_blocked_ip(ip_str: str) -> Tuple[bool, str]:
    """Return ``(blocked, reason)``.

    Blocks all loopback / private / link-local / multicast / reserved /
    unspecified ranges plus the AWS/GCP/Azure metadata service IP.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True, f"unparseable IP: {ip_str}"

    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return True, "cloud metadata address"
    if ip.is_loopback:
        return True, "loopback address"
    if ip.is_private:
        return True, "private address"
    if ip.is_link_local:
        return True, "link-local address"
    if ip.is_multicast:
        return True, "multicast address"
    if ip.is_reserved:
        return True, "reserved address"
    if ip.is_unspecified:
        return True, "unspecified address"
    return False, ""


def validate_provider_url(
    url: str,
    *,
    allow_custom: bool = True,
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_PROVIDER_HOSTS,
    allowed_schemes: Iterable[str] = ("http", "https"),
) -> SafeUrl:
    """Validate a user-supplied provider URL and return a sanitized form.

    Raises ``UrlSafetyError`` if the URL is malformed, uses a disallowed
    scheme, contains userinfo or a fragment, resolves to a blocked IP
    range, or (when ``allow_custom=False``) is not in ``allowed_hosts``.

    The returned ``sanitized`` URL has its userinfo, query string, and
    fragment removed. Callers should append fixed provider paths
    (``/models``, ``/api/tags``, ...) onto ``sanitized`` rather than
    onto the original input.
    """
    if not url or not isinstance(url, str):
        raise UrlSafetyError("url is required")

    parsed = urlparse(url.strip())

    if parsed.scheme not in set(allowed_schemes):
        raise UrlSafetyError(f"scheme not allowed: {parsed.scheme or '(missing)'}")

    if not parsed.hostname:
        raise UrlSafetyError("url is missing a host")

    if parsed.username or parsed.password:
        raise UrlSafetyError("url must not include userinfo")

    if parsed.fragment:
        raise UrlSafetyError("url must not include a fragment")

    host = parsed.hostname.lower()
    allowed_set = {h.lower() for h in allowed_hosts}
    is_allowlisted = host in allowed_set

    # Public-allowlist hosts skip DNS-based blocking — the host name
    # itself is the trust anchor. (We still strip userinfo and query
    # string below.) Anything else requires both ``allow_custom`` and
    # passing the IP-range check.
    if not is_allowlisted:
        if not allow_custom:
            raise UrlSafetyError(f"host not in allowlist: {host}")

        for addr in _resolve_host(host):
            blocked, reason = _is_blocked_ip(addr)
            if blocked:
                raise UrlSafetyError(f"resolved address {addr} is disallowed: {reason}")

    # Reconstruct a sanitized URL (no userinfo, no query, no fragment).
    # Keep the path so the caller can use the original base path; we
    # strip the query string explicitly because the disclosure showed
    # that ``base_url=http://x/foo?proof=`` produced a request to
    # ``/foo?proof=/models`` after the appended ``/models``.
    sanitized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc.split("@")[-1],  # drop userinfo if anything slipped through
            parsed.path or "",
            "",  # params
            "",  # query
            "",  # fragment
        )
    )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    return SafeUrl(
        sanitized=sanitized,
        host=host,
        port=port,
        scheme=parsed.scheme,
        is_allowlisted_host=is_allowlisted,
    )


def safe_provider_base_url(
    url: Optional[str],
    default: str,
    *,
    allow_custom: bool = True,
) -> SafeUrl:
    """Convenience: validate ``url`` if given, otherwise the ``default``.

    The default is assumed to be a trusted provider URL (it lives in
    code, not request input) but we still pipe it through the same
    validator so the returned object has a consistent shape.
    """
    target = url.strip() if (url and url.strip()) else default
    return validate_provider_url(target, allow_custom=allow_custom)
