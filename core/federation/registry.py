"""Adapter registry for federated monitoring.

Each adapter wraps one external data source (Splunk, CrowdStrike, etc.) behind
a uniform fetch interface so the runner in :mod:`daemon.poller` can iterate
over a registry instead of hardcoding per-source loops.

The adapter contract itself (``FetchResult``, ``FederationAdapter``,
``register_adapter``) lives in :mod:`core.federation.contract` so adapter
modules can import it without depending on this module — which lazily imports
every adapter in :func:`_ensure_builtins_loaded` and would otherwise form an
import cycle.
"""

from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Optional

from core.config import load_integrations_config
from core.federation.contract import _ADAPTER_FACTORIES, FederationAdapter
from core.integrations.integration_bridge_service import base_of, instances

logger = logging.getLogger(__name__)

_BUILTIN_ADAPTER_MODULES = (
    "core.integrations.aws_security_hub.adapter",
    "core.integrations.azure_sentinel.adapter",
    "core.integrations.crowdstrike.adapter",
    "core.integrations.elastic.adapter",
    "core.integrations.microsoft_defender.adapter",
    "core.integrations.opensearch.adapter",
    "core.integrations.splunk.adapter",
)


def _build(name: str, *args: str) -> Optional[FederationAdapter]:
    try:
        return _ADAPTER_FACTORIES[name](*args)
    except Exception as e:
        logger.warning(
            "Federation adapter %s failed to construct: %s",
            args[0] if args else name,
            e,
        )
        return None


def instance_sources() -> Dict[str, str]:
    """Each saved, enabled instance (#5353) -> the registry key of its base's adapter.

    Adapters are keyed by source name (``microsoft_defender``), instances by
    integration id (``microsoft-defender-eu``), so the base is matched on the
    adapter's ``integration_id``. Raises when the config cannot be read.
    """
    _ensure_builtins_loaded()
    rows = load_integrations_config()
    enabled = set(rows["enabled_integrations"])
    found = instances(rows["integrations"])
    if not any(iid in enabled for iid in found):
        return {}
    keys: Dict[str, str] = {}
    for name in _ADAPTER_FACTORIES:
        adapter = _build(name)
        if adapter is not None:
            keys[getattr(adapter, "integration_id", name)] = name
    return {
        iid: keys[base]
        for iid, base in found.items()
        if iid in enabled and base in keys
    }


def _instance_key(name: str) -> Optional[str]:
    """The base adapter's key when ``name`` is a saved, enabled instance."""
    if base_of(name) is None:  # no read for a name that cannot be an instance
        return None
    try:
        return instance_sources().get(name)
    except Exception as e:
        logger.warning("Federation could not read instances: %s", e)
        return None


def list_adapters() -> List[FederationAdapter]:
    """Instantiate every registered adapter, then one per enabled instance.

    Adapters that fail to instantiate are skipped with a warning so one bad
    integration can't break the rest of the federation poller.
    """
    _ensure_builtins_loaded()
    out = [a for a in map(_build, list(_ADAPTER_FACTORIES)) if a is not None]
    try:
        found = instance_sources()
    except Exception as e:
        logger.warning("Federation could not read instances: %s", e)
        found = {}
    out += [a for iid, key in found.items() if (a := _build(key, iid)) is not None]
    return out


def is_registered(name: str) -> bool:
    """True if ``name`` is a registered adapter or an enabled instance of one."""
    _ensure_builtins_loaded()
    return name in _ADAPTER_FACTORIES or _instance_key(name) is not None


def get_adapter(name: str) -> Optional[FederationAdapter]:
    """Look up a single adapter by source_id (an instance's is its id)."""
    _ensure_builtins_loaded()
    if name in _ADAPTER_FACTORIES:
        return _build(name)
    key = _instance_key(name)
    return _build(key, name) if key else None


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
