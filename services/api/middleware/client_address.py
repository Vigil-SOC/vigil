"""Client address for rate-limit keys.

The TCP peer is the client unless it is a configured proxy. A proxy may set
X-Forwarded-For; the client is the nearest hop that is not itself trusted.
Entries further left are whatever the client sent, so they are not the key.
"""

import ipaddress
import logging
from functools import lru_cache

from starlette.requests import Request

from core.config import get_settings

logger = logging.getLogger(__name__)


class _Trusted:
    def __init__(self) -> None:
        self.ips: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.nets: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

    def __bool__(self) -> bool:
        return bool(self.ips or self.nets)

    def covers(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip in self.ips:
            return True
        return any(ip in net for net in self.nets)


@lru_cache(maxsize=8)
def _compile(raw: str) -> _Trusted:
    trusted = _Trusted()
    for index, item in enumerate(raw.split(","), start=1):
        text = item.strip()
        if not text:
            continue
        if text == "*":
            logger.error("VIGIL_TRUSTED_PROXIES ignores '*'; name the proxy addresses")
            continue
        try:
            if "/" in text:
                trusted.nets.add(ipaddress.ip_network(text, strict=False))
            else:
                trusted.ips.add(ipaddress.ip_address(text))
        except ValueError:
            # The entry itself stays out of the log: it is operator config.
            logger.error("VIGIL_TRUSTED_PROXIES ignores unparseable entry %d", index)
    return trusted


def _host_of(value: str) -> str:
    """Host from a forwarded hop. Bare IPv6 (more than one colon) is kept whole."""
    value = value.strip().strip('"')
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if value.count(":") == 1:
        host, _, port = value.rpartition(":")
        if port.isdigit():
            return host
    return value


def client_host(request: Request) -> str:
    """Address a rate limit should treat as the client."""
    peer = request.client.host if request.client and request.client.host else "unknown"
    trusted = _compile(get_settings().vigil_trusted_proxies or "")
    if not trusted or not trusted.covers(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    hops = [_host_of(part) for part in forwarded.split(",") if part.strip()]
    hops = [hop for hop in hops if hop]
    if not hops:
        return peer
    for hop in reversed(hops):
        if not trusted.covers(hop):
            return hop
    return hops[0]
