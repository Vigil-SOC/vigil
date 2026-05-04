"""Adapter contract + registry for federated monitoring.

Each adapter wraps one external data source (Splunk, CrowdStrike, etc.) behind
a uniform fetch interface so the runner in :mod:`daemon.poller` can iterate
over a registry instead of hardcoding per-source loops.

Adapters are intentionally thin — they delegate to existing services in
``services/*`` for the actual API calls. The contract here only normalizes:

  * how the runner detects whether the underlying integration is configured
    (so we can skip seeding/polling sources the user hasn't set up),
  * the fetch shape (cursor in, findings + new cursor out),
  * the default cadence each source class warrants (EDR is faster than SIEM).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """One adapter fetch's output.

    ``findings`` is the list of normalized finding dicts (same shape the rest
    of the daemon already consumes — see ``services.ingestion_service``).
    ``cursor`` is the new persisted cursor for the next fetch — the runner
    writes it to ``federation_sources.cursor`` after a successful fetch.
    """

    findings: List[Dict[str, Any]] = field(default_factory=list)
    cursor: Dict[str, Any] = field(default_factory=dict)


class FederationAdapter(Protocol):
    """Contract every federated-monitoring source adapter implements."""

    #: Unique source identifier (also the federation_sources.source_id PK).
    name: str

    def is_configured(self) -> bool:
        """True if the underlying integration is configured (e.g. credentials set)."""
        ...

    def default_interval(self) -> int:
        """Default poll interval (seconds). Used when seeding a fresh row."""
        ...

    async def fetch(
        self,
        *,
        since: Optional[datetime],
        cursor: Dict[str, Any],
        max_items: int,
    ) -> FetchResult:
        """Pull new findings since the cursor.

        ``since`` is an absolute fallback for adapters that don't honor the
        cursor on first run; ``cursor`` is the per-source state previously
        returned by this adapter (empty dict on first run — adapters MUST
        treat empty as "from now", we do not backfill on cold start).

        Implementations should honor ``max_items`` to bound first-run blast
        radius if the source happens to return everything in its history.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Adapter constructors are registered lazily so importing this module doesn't
# pull in every service. The runner calls ``list_adapters()`` once at startup.
_ADAPTER_FACTORIES: Dict[str, Callable[[], FederationAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], FederationAdapter]) -> None:
    """Register an adapter factory under ``name``.

    Called once per adapter module at import time. Re-registration overwrites
    the prior factory (useful in tests).
    """
    _ADAPTER_FACTORIES[name] = factory


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
