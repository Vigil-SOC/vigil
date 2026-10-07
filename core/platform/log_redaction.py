"""Redaction for values that must be named in a log line but never spelled out."""

from urllib.parse import urlsplit, urlunsplit


def redact_url(url: str) -> str:
    """The URL with any userinfo (``user:password@``) removed.

    Scheme, host, port and path are kept, so the line still says where a client
    connected. A string that does not parse as a URL is replaced outright rather
    than echoed, since it may be a bare credential.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:  # IPv6 literal lost its brackets in .hostname
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port else host
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme or not netloc:
        return "<unparseable url>"
    # Query and fragment can carry tokens too (e.g. ?password=...).
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def mask_email(address: str) -> str:
    """``alice@example.com`` -> ``a***@example.com``: enough to tell addresses apart in ops."""
    local, sep, domain = address.partition("@")
    if not sep or not local:
        return "***"
    return f"{local[0]}***@{domain}"
