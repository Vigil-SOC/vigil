"""Integration descriptor — the per-vendor source of truth for registry metadata.

Vendors register a descriptor here; the scattered integration registries
(secret-field map, MCP-server map, …) derive their per-vendor entries from it
instead of hardcoding them.

Descriptors are found by scanning ``core/integrations/*/descriptor.py``, not by
an import list. A descriptor missing from such a list never runs its
``register_descriptor`` call and goes silently dead — which is exactly how
darktrace's became dead scaffolding before #557 removed it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

_SLICES_DIR = Path(__file__).resolve().parent.parent

_TRUTHY = {"true", "1", "yes", "on"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_COERCERS: Dict[str, Callable[[Any], Any]] = {
    "str": lambda value: value,
    "bool": _to_bool,
    "int": _to_int,
}


@dataclass(frozen=True)
class IntegrationField:
    """One configurable value: its name, whether it is secret, its type.

    ``value_type`` exists because the two config channels disagree on typing.
    The Settings form stores JSON, so a checkbox arrives as a real ``bool``;
    the env channel — an ``mcp-config.json`` ``env`` block, or ``get_secret``
    — is strings all the way down. Undeclared, ``VERIFY_SSL=false`` reaches
    ``requests(verify=...)`` as the string ``"false"``, which is not a flag
    but a CA-bundle path that does not exist.
    """

    name: str
    secret: bool = False
    value_type: str = "str"
    # What an unset field resolves to; ``None`` leaves the caller to decide.
    default: Any = None

    def __post_init__(self) -> None:
        if self.value_type not in _COERCERS:
            raise ValueError(
                f"{self.name}: unknown value_type {self.value_type!r}, "
                f"expected one of {sorted(_COERCERS)}"
            )

    def coerce(self, value: Any) -> Any:
        """Apply the declared type. Unset is the field's default, ``None`` unless declared."""
        if value is None:
            return self.default
        return _COERCERS[self.value_type](value)


@dataclass(frozen=True)
class TaxiiFeed:
    """A TAXII 2.1 feed an integration serves into ``threat_indicators``.

    Built from the integration's resolved config by its descriptor's
    ``taxii_feed``. The proxy is not here: the poller reads the integration's
    own with ``integration_proxy(descriptor.id)``.
    """

    server_url: Optional[str]
    api_token: Optional[str]
    collection_ids: Tuple[str, ...]
    verify_ssl: bool = True
    ca_cert_path: Optional[str] = None
    # Saved poll interval in seconds; the poller takes the smallest across feeds.
    poll_interval_seconds: Optional[int] = None

    @property
    def requests_verify(self) -> Union[bool, str]:
        """``verify`` for ``requests`` (taxii2client): False, a CA path, or True."""
        if not self.verify_ssl:
            return False
        return self.ca_cert_path or True


def parse_collection_ids(raw: Any) -> Tuple[str, ...]:
    """A saved comma-separated collection id list, blanks dropped."""
    return tuple(c.strip() for c in str(raw or "").split(",") if c.strip())


@dataclass(frozen=True)
class IntegrationDescriptor:
    """One code-backed integration's registry facts.

    ``fields`` is the *complete* field list, not just the secret ones: the
    config resolver keys its result off it, so a server can only read what its
    descriptor declares.

    ``mcp_server_names`` is a tuple because one vendor can back several servers
    — Splunk has both an official server and the self-hosted one Vigil ships.
    """

    id: str
    category: str
    mcp_server_names: Tuple[str, ...] = ()
    fields: Tuple[IntegrationField, ...] = ()
    # Optional console URL. ``{external_id}`` and the descriptor's non-secret
    # field names. Set only where the stored ``external_id`` is the vendor's
    # own alert id and the vendor documents the URL; cite the doc beside it.
    # Evidence refs win when they are links.
    console_link_template: Optional[str] = None
    # Set when the integration can serve a TAXII feed: takes its resolved
    # config and returns the feed, or None when no collection ids are saved.
    taxii_feed: Optional[Callable[[Dict[str, Any]], Optional[TaxiiFeed]]] = None

    @property
    def secret_fields(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.secret)

    @property
    def field_names(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields)


_REGISTRY: Dict[str, IntegrationDescriptor] = {}
_discovered = False
_discovering = False


def register_descriptor(descriptor: IntegrationDescriptor) -> IntegrationDescriptor:
    _REGISTRY[descriptor.id] = descriptor
    return descriptor


def _discover() -> None:
    """Import every vendor slice's descriptor module exactly once.

    ``_discovering`` guards re-entry so a descriptor that reaches back into
    this module can't recurse; ``_discovered`` is only set once the sweep
    completes. Import errors propagate: a broken descriptor should fail loudly
    rather than leave a vendor silently unregistered. Marking the sweep done up
    front would do exactly that — a caller that swallows the error would pin a
    partial registry in place for the life of the process, and every secret
    field belonging to a missing vendor would then be persisted in plaintext.
    """
    global _discovered, _discovering
    if _discovered or _discovering:
        return
    _discovering = True
    try:
        for path in sorted(_SLICES_DIR.glob("*/descriptor.py")):
            package = path.parent.name
            if package.startswith("_"):
                continue
            importlib.import_module(f"core.integrations.{package}.descriptor")
        _discovered = True
    finally:
        _discovering = False


def get_descriptor(integration_id: str) -> Optional[IntegrationDescriptor]:
    _discover()
    return _REGISTRY.get(integration_id)


def iter_descriptors() -> Tuple[IntegrationDescriptor, ...]:
    _discover()
    return tuple(_REGISTRY.values())
