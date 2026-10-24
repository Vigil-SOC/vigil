"""Read-side client for Bifrost's logging API (#185).

Bifrost is the authoritative cost source for every LLM call Vigil makes —
its logging plugin records exact cost against current pricing for every
upstream request, with built-in batch-recompute for retroactive repricing.
This module is the one place the backend talks to that read-side API,
mirroring the pattern set by ``core.llm.bifrost.admin`` (module-level
functions, env-driven base URL, ``httpx.Client``, failures returned as
``None``/empty rather than raised).

What's wrapped (verified against docs.getbifrost.ai):

  * ``GET /api/governance/virtual-keys/quota``, per-key quota (``get_vk_quota``)

What's *not* wrapped here: provider config (``core.llm.bifrost.admin``
already owns provider key/model writes) and the rest of the governance/budget
endpoints (those land in PR C / #186 alongside the VK enforcement work).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from core.config import get_settings
from core.llm.bifrost.auth import management_auth

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0


def _bifrost_base_url() -> str:
    return get_settings().bifrost_url.rstrip("/")


# ---------------------------------------------------------------------------
# Internal HTTP helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Governance / virtual-key quota (#186)
# ---------------------------------------------------------------------------


def get_vk_quota(vk: str) -> Optional[Dict[str, Any]]:
    """Return the budget + rate-limit quota for a virtual key.

    Path: ``GET /api/governance/virtual-keys/quota``. The management API
    requires the admin password; ``x-bf-vk`` still names the key (the same
    header dispatch attaches on every upstream call). Response shape:

    .. code-block:: json

        {
          "virtual_key_name": "...",
          "is_active": true,
          "budgets": [
            {"id": "...", "max_limit": 100.0, "current_usage": 42.0,
             "reset_duration": "1mo", "calendar_aligned": true,
             "last_reset": "..."}
          ],
          "rate_limit": {"id": "...", "token_max_limit": 1000000,
                         "token_current_usage": 12345, ...}
        }

    The Settings › AI models spending limit card uses this directly
    to render "$X of $Y consumed" without round-tripping through Vigil's
    aggregations.
    """
    if not vk:
        return None
    return _get_json(
        "/api/governance/virtual-keys/quota",
        headers={"x-bf-vk": vk},
    )


def _get_json(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{_bifrost_base_url()}{path}"
    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT, auth=management_auth()) as client:
            r = client.get(url, params=params or {}, headers=headers or {})
            if r.status_code >= 400:
                logger.warning(
                    "bifrost_cost_client: GET %s returned %s: %s",
                    path,
                    r.status_code,
                    r.text[:200],
                )
                return None
            return r.json()
    except Exception as e:
        logger.warning("bifrost_cost_client: GET %s failed: %s", path, e)
        return None
