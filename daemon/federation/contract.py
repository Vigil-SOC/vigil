"""Adapter contract + registration primitives for federated monitoring.

Split out from :mod:`daemon.federation.registry` so adapter modules can import
the contract (``FetchResult``, ``FederationAdapter``, ``register_adapter``)
without depending on the registry — which in turn lazily imports every adapter
in :func:`daemon.federation.registry._ensure_builtins_loaded`. Keeping the
contract here (a module that imports nothing from ``daemon.federation``) breaks
what would otherwise be an adapter <-> registry import cycle.

The registry re-exports these names, so existing
``from daemon.federation.registry import FetchResult`` imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol


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
# Registration
# ---------------------------------------------------------------------------

# Adapter constructors are registered lazily so importing the registry doesn't
# pull in every service. The runner calls ``list_adapters()`` once at startup.
_ADAPTER_FACTORIES: Dict[str, Callable[[], FederationAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], FederationAdapter]) -> None:
    """Register an adapter factory under ``name``.

    Called once per adapter module at import time. Re-registration overwrites
    the prior factory (useful in tests).
    """
    _ADAPTER_FACTORIES[name] = factory
