"""One address's canonical text form, and the finding fields that hold one."""

import ipaddress
from typing import Any, Optional

# Top-level entity_context keys ingest writes addresses under. Each holds a
# string or a list of strings.
FINDING_IP_KEYS = (
    "src_ip",
    "src_ips",
    "source_ip",
    "source_ips",
    "srcip",
    "dst_ip",
    "dst_ips",
    "dest_ip",
    "dest_ips",
    "destination_ip",
    "destination_ips",
    "dstip",
    "ip",
    "ips",
    "ip_address",
    "ip_addresses",
    "local_ip",
    "remote_ip",
    "client_ip",
)


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


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return normalize_ip(value) or value


def canonical_entity_context(entity_context: Any) -> Any:
    """``entity_context`` with every address under :data:`FINDING_IP_KEYS` in
    canonical form. Values that are not an address are kept as they are."""
    if not isinstance(entity_context, dict):
        return entity_context
    return {
        key: _canonical(value) if key in FINDING_IP_KEYS else value
        for key, value in entity_context.items()
    }
