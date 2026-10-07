"""Adapter registry for federated monitoring.

Each adapter wraps one external data source (Splunk, CrowdStrike, etc.) behind
a uniform fetch interface so the runner in :mod:`daemon.poller` can iterate
over a registry instead of hardcoding per-source loops.

The adapter contract itself (``FetchResult``, ``FederationAdapter``,
``register_adapter``) lives in :mod:`core.federation.contract` so adapter
modules can import it without depending on this module — which lazily imports
every adapter in :func:`_ensure_builtins_loaded` and would otherwise form an
import cycle. Those names are re-exported here for backward compatibility.
"""

from __future__ import annotations

import importlib
import logging
from typing import List, Optional

from core.federation.contract import (
    _ADAPTER_FACTORIES,
    FederationAdapter,
    FetchResult,
    register_adapter,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FederationAdapter",
    "FetchResult",
    "register_adapter",
    "list_adapters",
    "get_adapter",
    "is_registered",
]

_BUILTIN_ADAPTER_MODULES = (
    "core.integrations.aws_security_hub.adapter",
    "core.integrations.azure_sentinel.adapter",
    "core.integrations.crowdstrike.adapter",
    "core.integrations.elastic.adapter",
    "core.integrations.microsoft_defender.adapter",
    "core.integrations.opensearch.adapter",
    "core.integrations.splunk.adapter",
)


def list_adapters() -> List[FederationAdapter]:
    """Instantiate every registered adapter.

    Adapters that fail to instantiate are skipped with a warning so one bad
    integration can't break the rest of the federation poller.
    """
    _ensure_builtins_loaded()
    out: List[FederationAdapter] = []
    for name, factory in _ADAPTER_FACTORIES.items():
        try:
            out.append(factory())
        except Exception as e:
            logger.warning("Federation adapter %s failed to construct: %s", name, e)
    return out


def is_registered(name: str) -> bool:
    """True if an adapter factory is registered for ``name`` (nothing is built)."""
    _ensure_builtins_loaded()
    return name in _ADAPTER_FACTORIES


def get_adapter(name: str) -> Optional[FederationAdapter]:
    """Look up a single adapter by source_id."""
    _ensure_builtins_loaded()
    factory = _ADAPTER_FACTORIES.get(name)
    if factory is None:
        return None
    try:
        return factory()
    except Exception as e:
        logger.warning("Federation adapter %s failed to construct: %s", name, e)
        return None


_BUILTINS_LOADED = False


def _ensure_builtins_loaded() -> None:
    """Import the builtin adapter modules so they self-register.

    We do this lazily (rather than at module import) to avoid pulling in every
    SIEM/EDR service when something like a unit test only wants the registry
    type definitions.
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    # Import for side effects (each module calls register_adapter at module
    # scope). One try per module so a broken adapter can't unregister the rest.
    for module in _BUILTIN_ADAPTER_MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            logger.error(
                "Failed to load federation adapter %s; its source will not be "
                "polled by federation",
                module,
                exc_info=True,
            )
