"""Adapter registry for federated monitoring.

Each adapter wraps one external data source (Splunk, CrowdStrike, etc.) behind
a uniform fetch interface so the runner in :mod:`daemon.poller` can iterate
over a registry instead of hardcoding per-source loops.

The adapter contract itself (``FetchResult``, ``FederationAdapter``,
``register_adapter``) lives in :mod:`daemon.federation.contract` so adapter
modules can import it without depending on this module — which lazily imports
every adapter in :func:`_ensure_builtins_loaded` and would otherwise form an
import cycle. Those names are re-exported here for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from daemon.federation.contract import (
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
]


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
            logger.warning(
                "Federation adapter %s failed to construct: %s", name, e
            )
    return out


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
    # Import for side effects (each module calls register_adapter at module scope).
    try:
        from daemon.federation.adapters import (  # noqa: F401
            aws_security_hub,
            azure_sentinel,
            crowdstrike,
            elastic,
            microsoft_defender,
            splunk,
        )
    except Exception as e:
        logger.warning("Failed to load builtin federation adapters: %s", e)
