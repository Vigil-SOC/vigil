"""Federated monitoring for the SOC daemon.

Each adapter under :mod:`core.federation.adapters` polls one external data
source on a configurable cadence and yields normalized findings. The
:mod:`core.federation.registry` module enumerates available adapters, and
:func:`core.federation.seed.seed_federation_sources` ensures a row exists in
``federation_sources`` for every adapter whose underlying integration is
configured (default disabled, opt-in).
"""

from core.federation.registry import (
    FederationAdapter,
    FetchResult,
    get_adapter,
    list_adapters,
)

__all__ = [
    "FederationAdapter",
    "FetchResult",
    "get_adapter",
    "list_adapters",
]
