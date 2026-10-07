"""One address's canonical text form. Dependency-free so the memory vocabulary
and the exclusion store agree on spelling without importing each other."""

import ipaddress
from typing import Any, Optional


def normalize_ip(value: Any) -> Optional[str]:
    """Canonical text form of one address, or ``None`` when it is not one.

    Accepts surrounding whitespace and IPv6 brackets. Refuses networks and
    ports: an exclusion names one address.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return None
