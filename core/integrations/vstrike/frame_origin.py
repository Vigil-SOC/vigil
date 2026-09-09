"""Resolve the configured embedding origin without returning credentials."""

import ipaddress
import re
from urllib.parse import urlsplit

from core.config import get_integration_config
from core.secrets import get_secret


def configured_frame_origin() -> str | None:
    """Only a configured HTTP(S) origin may be added to the default frame policy."""
    try:
        config = get_integration_config("vstrike") or {}
        value = get_secret("VSTRIKE_BASE_URL") or config.get("url")
        if not isinstance(value, str):
            return None
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host:
            return None
        if parsed.username or parsed.password:
            return None
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host)}]"
        elif not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            return None
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except (ValueError, TypeError):
        return None
