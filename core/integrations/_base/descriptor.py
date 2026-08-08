"""Integration descriptor — the per-vendor source of truth for registry metadata.

Vendors register a descriptor here; the scattered integration registries
(secret-field map, MCP-server map, …) derive their per-vendor entries from it
instead of hardcoding them. Splunk is the proof-of-concept (#483); #484
generalizes the derivation across every vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class IntegrationField:
    name: str
    env_suffix: Optional[str] = None
    secret: bool = False


@dataclass(frozen=True)
class IntegrationDescriptor:
    id: str
    category: str
    mcp_server_name: Optional[str] = None
    fields: Tuple[IntegrationField, ...] = ()

    @property
    def secret_fields(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.secret)


_REGISTRY: Dict[str, IntegrationDescriptor] = {}


def register_descriptor(descriptor: IntegrationDescriptor) -> IntegrationDescriptor:
    _REGISTRY[descriptor.id] = descriptor
    return descriptor


def get_descriptor(integration_id: str) -> Optional[IntegrationDescriptor]:
    return _REGISTRY.get(integration_id)


def iter_descriptors() -> Tuple[IntegrationDescriptor, ...]:
    return tuple(_REGISTRY.values())
