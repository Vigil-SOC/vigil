"""Where a provider's calls go, and the one label for it.

The only implementation of the provider class. The provider API, the gateway
routability read and (later) the model-processing check all call it; the
console renders the class it returns and never works one out itself.

An address is never evidence that a provider is self-hosted: a provider whose
every address is on a private network is ``local_by_address`` — local by
address, not confirmed. The local test is string rules only (no DNS lookup):
``core/platform/url_safety.py`` resolves names and treats private hosts as
blocked, which is the opposite question.

Nothing here reads a secret *value*. The only stored field read is Vigil's own
copy of an Ollama key's URL (``refs.ollama_ref``), because the gateway masks it.
The addresses stay server-side: ``Residency.public`` is what a payload carries.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from core.config import get_settings
from core.llm.bifrost.refs import ollama_ref
from core.secrets import get_secret

ResidencyClass = Literal["local_by_address", "your_cloud", "hosted", "unknown"]

LOCAL_BY_ADDRESS = "local_by_address"
YOUR_CLOUD = "your_cloud"
HOSTED = "hosted"
UNKNOWN = "unknown"

# The models run in the customer's own cloud account, wherever the host is.
YOUR_CLOUD_TYPES = frozenset({"bedrock", "vertex", "azure"})

# The vendor's own host, as a fixed per-type token: no URL to read, and
# setting a custom host later changes the address set.
STOCK_PREFIX = "stock:"

REASON_GATEWAY_UNREADABLE = "The gateway couldn't be read."
REASON_KEYS_UNREADABLE = "The gateway couldn't list this provider's keys."
REASON_NOT_IN_GATEWAY = "This provider isn't in the gateway."
REASON_NO_KEY = "It has no enabled key, so there's no server address to read."
REASON_MASKED_URL = (
    "A key was set up outside Vigil, and the gateway masks its server URL."
)


_V6_LINK_LOCAL = ipaddress.IPv6Network("fe80::/10")


@dataclass(frozen=True)
class Residency:
    addresses: FrozenSet[str]
    cls: ResidencyClass
    reason: Optional[str] = None

    @property
    def address_known(self) -> bool:
        return self.reason is None

    def public(self) -> Dict[str, Any]:
        """What a payload carries. Never the addresses."""
        return {
            "class": self.cls,
            "address_known": self.address_known,
            "reason": self.reason,
        }


def url_is_private_host(url: str) -> bool:
    """Python twin of ``urlIsPrivateHost`` in clients/web/src/shared/loopback.ts.

    True for loopback, RFC 1918, link-local, ``host.docker.internal``, a
    dotless name, or a ``.local`` / ``.internal`` name. A stock token, a parse
    failure or an empty string is false.
    """
    if "\\" in url:
        return False  # urlparse and the browser split a backslash differently
    try:
        parsed = urlparse(url.strip())
        parsed.port  # an out-of-range or non-numeric port fails the parse, as in TS
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address):
        # ::1 and fe80::/10 only; an IPv4-mapped address is not unwrapped
        return ip == ipaddress.IPv6Address("::1") or ip in _V6_LINK_LOCAL
    if isinstance(ip, ipaddress.IPv4Address):
        a, b = ip.packed[0], ip.packed[1]
        return (
            a in (10, 127)
            or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168)
            or (a == 169 and b == 254)
        )
    if re.fullmatch(r"[\d.]+", host):
        return False  # malformed dotted quad: the browser's parser rejects it too
    return (
        host in ("localhost", "host.docker.internal")
        or "." not in host
        or host.endswith((".local", ".internal"))
    )


def _classify(
    provider_type: str, addresses: Iterable[str], reason: Optional[str] = None
) -> Residency:
    addrs = frozenset(a for a in addresses if a)  # an empty OLLAMA_URL is no address
    if provider_type in YOUR_CLOUD_TYPES:
        cls: ResidencyClass = YOUR_CLOUD
    elif reason is not None or not addrs:
        # Unknown never falls through to local.
        cls, reason = UNKNOWN, reason or REASON_NO_KEY
    elif all(url_is_private_host(a) for a in addrs):
        cls = LOCAL_BY_ADDRESS
    else:
        cls = HOSTED
    return Residency(addrs, cls, reason)


def _ollama_key_address(key: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(address, reason) for one Ollama key; exactly one is set."""
    raw = (key.get("ollama_key_config") or {}).get("url")
    ref = None
    if isinstance(raw, dict):
        ref = raw.get("ref") or raw.get("env_var")
    elif isinstance(raw, str) and raw.startswith("env."):
        ref = raw
    stored = get_secret(ollama_ref(key["id"])) if key.get("id") else None
    if not ref and stored and stored.startswith("env."):
        ref = stored
    if ref:
        name = ref.removeprefix("env.")
        if name == "OLLAMA_URL":
            return get_settings().ollama_url, None
        return None, f"A key reads its server URL from {name}, which Vigil can't see."
    if stored:
        return stored, None
    return None, REASON_MASKED_URL


def gateway_provider_residency(
    doc: Dict[str, Any], keys: Optional[List[Dict[str, Any]]]
) -> Residency:
    """One gateway provider, from its document and its keys.

    ``keys`` is None when the gateway could not list them; that only matters
    for Ollama, whose address lives on the key.
    """
    name = doc.get("name") or ""
    custom = doc.get("custom_provider_config") or {}
    ptype = name if not custom else custom.get("base_provider_type") or name
    base_url = (doc.get("network_config") or {}).get("base_url")
    if base_url:
        return _classify(ptype, [base_url])
    if ptype != "ollama":
        return _classify(ptype, [f"{STOCK_PREFIX}{ptype}"])
    if keys is None:
        return _classify(name, [], REASON_KEYS_UNREADABLE)
    addresses: List[str] = []
    for key in keys:
        if not key.get("enabled", True):
            continue
        address, reason = _ollama_key_address(key)
        if reason:
            return _classify(name, [], reason)
        addresses.append(address)  # type: ignore[arg-type]
    return _classify(name, addresses)


def is_gateway_row(row: Any) -> bool:
    return ((getattr(row, "config", None) or {}).get("managed_by")) == "bifrost"


def row_residency(
    row: Any,
    gateway: Optional[Dict[str, Residency]],
    gateway_reason: str = REASON_GATEWAY_UNREADABLE,
) -> Residency:
    """One ``llm_provider_configs`` row.

    ``gateway`` is every gateway provider's residency by name, or None when
    the gateway could not be read. A mirror row is keyed by its provider type,
    not by being active: a deactivated row's provider is still on the screen.
    """
    ptype = row.provider_type
    if row.base_url:
        return _classify(ptype, [row.base_url])
    if is_gateway_row(row):
        if gateway is None:
            return _classify(ptype, [], gateway_reason)
        return gateway.get(ptype) or _classify(ptype, [], REASON_NOT_IN_GATEWAY)
    if ptype == "ollama":
        # The default local provider: the address the server uses for Ollama.
        return _classify(ptype, [get_settings().ollama_url])
    return _classify(ptype, [f"{STOCK_PREFIX}{ptype}"])


async def gateway_residency() -> Optional[Dict[str, Residency]]:
    """Every gateway provider's residency, from the shared routability
    snapshot (at most 30s old); None when the gateway could not be read."""
    from core.llm.bifrost import path_health

    snap = await path_health.read()
    if snap["routability_error"] is not None or not snap["routability"]:
        return None if snap["routability_error"] is not None else {}
    return snap["routability"].get("residency") or {}


async def provider_residency(row: Any) -> Residency:
    """Where one provider row's calls go, and its class."""
    return row_residency(row, await gateway_residency())


class ProviderResidency(BaseModel):
    """``Residency.public`` as a response model, shared by the provider API
    and the gateway routability read so the generated client has one type."""

    model_config = ConfigDict(populate_by_name=True)

    class_: ResidencyClass = Field(alias="class")
    address_known: bool
    reason: Optional[str] = None
