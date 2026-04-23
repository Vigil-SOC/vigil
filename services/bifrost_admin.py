"""Bifrost management API helper.

Bifrost exposes a REST admin API at ``${BIFROST_URL}/api/providers/{name}``
that lets us update provider keys at runtime without a container restart.
This module is the one place the backend talks to that API, so the flow
is: user edits a key in the UI → ``llm_providers`` endpoint writes to the
secrets manager → this module pushes the new value to Bifrost → Bifrost
uses it for subsequent requests.

The previous architecture had Bifrost read ``env.ANTHROPIC_API_KEY`` from
its container env, which diverged from whatever the UI had written to the
secrets manager under ``llm_provider_<id>_api_key``. Pushing via the API
keeps a single source of truth in the secrets manager.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0


def _bifrost_base_url() -> str:
    return os.getenv("BIFROST_URL", "http://localhost:8080").rstrip("/")


def _get_provider(name: str, client: httpx.Client) -> Optional[Dict[str, Any]]:
    try:
        r = client.get(f"{_bifrost_base_url()}/api/providers/{name}", timeout=_DEFAULT_TIMEOUT)
        if r.status_code == 404:
            logger.debug("Bifrost: provider %s not configured", name)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Bifrost: could not fetch provider %s: %s", name, e)
        return None


def push_provider_key(provider_name: str, key_value: str) -> bool:
    """Update the first configured key on ``provider_name`` with ``key_value``.

    We take the current provider document, replace the key value with a
    literal (``from_env: false``), and PUT it back. This is idempotent —
    pushing the same value twice is a no-op from Anthropic's perspective.

    Returns True on success. Any failure is logged and returns False so
    the caller's CRUD flow never breaks on a Bifrost hiccup.
    """
    if not provider_name:
        return False
    with httpx.Client() as client:
        prov = _get_provider(provider_name, client)
        if prov is None:
            return False
        keys = prov.get("keys") or []
        if not keys:
            logger.warning("Bifrost: provider %s has no keys slot to update", provider_name)
            return False
        # Update the first key. Bifrost's current config seeds exactly one
        # key per provider; multi-key rotation is a separate feature.
        keys[0]["value"] = {
            "value": key_value,
            "env_var": "",
            "from_env": False,
        }
        try:
            r = client.put(
                f"{_bifrost_base_url()}/api/providers/{provider_name}",
                json=prov,
                timeout=_DEFAULT_TIMEOUT,
            )
            if r.status_code >= 400:
                logger.warning(
                    "Bifrost: PUT /api/providers/%s returned %s: %s",
                    provider_name, r.status_code, r.text[:200],
                )
                return False
            logger.info("Bifrost: pushed updated key for provider %s", provider_name)
            return True
        except Exception as e:
            logger.warning("Bifrost: PUT /api/providers/%s failed: %s", provider_name, e)
            return False


def sync_all_provider_keys() -> Dict[str, bool]:
    """Push every DB-configured provider's current secret value to Bifrost.

    Run on backend startup so Bifrost picks up whatever is in the secrets
    store regardless of how it was started or whether its container was
    recreated. Best-effort — returns a per-provider dict of success flags.
    """
    # Deferred imports to keep this module import-cheap for code that only
    # needs ``push_provider_key`` (e.g. llm_providers.py).
    from backend.secrets_manager import get_secret
    from database.connection import get_db_manager
    from database.models import LLMProviderConfig

    results: Dict[str, bool] = {}
    db_manager = get_db_manager()
    if db_manager._engine is None:
        db_manager.initialize()
    with db_manager.session_scope() as session:
        rows = session.query(LLMProviderConfig).filter(
            LLMProviderConfig.is_active.is_(True),
        ).all()
        for row in rows:
            if not row.api_key_ref:
                continue
            value = get_secret(row.api_key_ref)
            if not value:
                logger.debug(
                    "Bifrost sync: no value in secrets store for %s (ref=%s)",
                    row.provider_id, row.api_key_ref,
                )
                results[row.provider_id] = False
                continue
            results[row.provider_id] = push_provider_key(row.provider_type, value)
    if results:
        ok = sum(1 for v in results.values() if v)
        logger.info("Bifrost sync: pushed %d/%d provider keys", ok, len(results))
    return results
