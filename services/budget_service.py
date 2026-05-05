"""Bifrost virtual-key budget enforcement (#186).

The single point of truth for "which VK should this LLM call use" and
"should we enforce the budget right now". Two bypass envs cover the
free-tier / dev story:

  * ``DEV_MODE=true``        — already gates the rest of the auth stack;
                               implicitly disables budget enforcement so
                               local development isn't accidentally
                               blocked by a stale VK ceiling.
  * ``LLM_BUDGET_UNLIMITED=true`` — explicit budget bypass without
                               disabling auth (free-tier / sentinel VK).

Configuration lives in ``system_config['bifrost.virtual_keys']`` so
operators edit it from the Settings → LLM Providers → Budgets sub-panel.
The 60s runtime-config TTL the rest of Vigil uses applies here too.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

GLOBAL_KEY = "bifrost.virtual_keys"


# ---------------------------------------------------------------------------
# Typed exception — surfaces from llm_router when Bifrost returns 429/402
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Bifrost rejected an upstream call because a budget tier was hit.

    The chat UI catches this and renders a typed banner (instead of a
    generic 500 toast). The agent loop catches it as a terminal failure
    for the investigation rather than retrying.
    """

    def __init__(self, *, tier: str, message: str = "", status_code: Optional[int] = None):
        super().__init__(message or f"LLM budget exceeded ({tier})")
        self.tier = tier  # "virtual_key" | "team" | "customer" | "rate_limit" | "unknown"
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Bypass detection
# ---------------------------------------------------------------------------


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in ("true", "1", "yes")


def should_enforce() -> bool:
    """True if we should attach the VK header and respect Bifrost's gating.

    Returns False when DEV_MODE or LLM_BUDGET_UNLIMITED is on, OR when
    no default VK is configured (bootstrap window — accept calls without
    enforcement until the operator provisions a key).
    """
    if _env_truthy("DEV_MODE"):
        return False
    if _env_truthy("LLM_BUDGET_UNLIMITED"):
        return False
    if not get_active_vk():
        return False
    return True


# ---------------------------------------------------------------------------
# VK config lookup
# ---------------------------------------------------------------------------


def get_active_vk() -> Optional[str]:
    """Return the configured global VK ID, or None if not set.

    Lazily reads from ``system_config['bifrost.virtual_keys']``. Returns
    None on any DB error so a misconfigured persistence layer can never
    block LLM traffic — Vigil falls back to no-VK mode and the operator
    sees the bootstrap behavior.
    """
    settings = _get_settings()
    if not isinstance(settings, dict):
        return None
    vk = settings.get("default_vk")
    if not isinstance(vk, str) or not vk.strip():
        return None
    return vk.strip()


def get_settings() -> dict:
    """Public read of the full settings dict (for the Budgets UI)."""
    val = _get_settings() or {}
    if not isinstance(val, dict):
        return {}
    return {
        "default_vk": val.get("default_vk", "") or "",
        "budget_limit_usd": float(val.get("budget_limit_usd") or 0),
        "enforcement_mode": str(val.get("enforcement_mode") or "warning"),
    }


def set_settings(*, default_vk: str, budget_limit_usd: float, enforcement_mode: str) -> dict:
    """Persist the VK + budget config. Caller (API handler) is admin-gated."""
    if enforcement_mode not in ("warning", "hard_stop"):
        raise ValueError(
            f"enforcement_mode must be 'warning' or 'hard_stop', got {enforcement_mode!r}"
        )
    try:
        from database.config_service import get_config_service

        get_config_service(user_id="api").set_system_config(
            key=GLOBAL_KEY,
            value={
                "default_vk": default_vk or "",
                "budget_limit_usd": float(budget_limit_usd),
                "enforcement_mode": enforcement_mode,
            },
            description="Bifrost virtual-key configuration and budget settings",
            config_type="ai",
        )
    except Exception as e:
        logger.error("budget_service: failed to write settings: %s", e)
        raise
    return get_settings()


def _get_settings():
    try:
        from database.config_service import get_config_service

        return get_config_service().get_system_config(GLOBAL_KEY)
    except Exception as e:
        logger.debug("budget_service: settings read failed: %s", e)
        return None
