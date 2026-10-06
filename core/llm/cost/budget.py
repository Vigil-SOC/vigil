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
import threading
from typing import Literal, Optional, Tuple

from core.config import get_settings as get_app_settings
from core.storage.config_service import get_session
from core.storage.models import SystemConfig

logger = logging.getLogger(__name__)

GLOBAL_KEY = "bifrost.virtual_keys"

# Why a call does or does not carry x-bf-vk. Only ENFORCED attaches the key.
EnforcementReason = Literal[
    "enforced", "dev_mode", "unlimited", "not_configured", "read_error"
]
ENFORCED: EnforcementReason = "enforced"
DEV_MODE: EnforcementReason = "dev_mode"
UNLIMITED: EnforcementReason = "unlimited"
NOT_CONFIGURED: EnforcementReason = "not_configured"
READ_ERROR: EnforcementReason = "read_error"

# Last result of a settings read, so only the transition into and out of
# READ_ERROR is logged. Bypass reasons never read, so they leave it alone.
_state_lock = threading.Lock()
_last_read_reason: Optional[EnforcementReason] = None


# ---------------------------------------------------------------------------
# Typed exception — surfaces from router/router.py when Bifrost returns 429/402
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Bifrost refused an upstream call because a budget is spent.

    Raised on 402 and nothing else. A 429 means slow down and is retried by
    core.llm.gateway_retry, so reaching this exception always means waiting
    would not have helped.
    """

    def __init__(
        self, *, tier: str, message: str = "", status_code: Optional[int] = None
    ):
        super().__init__(message or f"LLM budget exceeded ({tier})")
        self.tier = tier  # "virtual_key" | "team" | "customer"
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Bypass detection
# ---------------------------------------------------------------------------


def _note_read(reason: EnforcementReason, exc: Optional[Exception] = None) -> None:
    """Record the outcome of a settings read; log on read_error transitions."""
    global _last_read_reason
    with _state_lock:
        previous, _last_read_reason = _last_read_reason, reason
    if reason == READ_ERROR and previous != READ_ERROR:
        why = (str(exc).splitlines() or [""])[0]
        logger.warning(
            "LLM budget enforcement skipped: settings read failed (%s: %s); "
            "calls are going out without x-bf-vk",
            type(exc).__name__,
            why,
        )
    elif previous == READ_ERROR and reason != READ_ERROR:
        logger.info("budget enforcement resumed (status: %s)", reason)


def enforcement_status() -> Tuple[EnforcementReason, Optional[str]]:
    """Return ``(reason, vk)``; ``vk`` is set only when ``reason`` is ENFORCED.

    Fails open: a failed settings read yields READ_ERROR (no key, calls are
    not blocked) instead of raising, and is logged once per transition.
    """
    app_settings = get_app_settings()
    if app_settings.dev_mode:
        return DEV_MODE, None
    if app_settings.llm_budget_unlimited:
        return UNLIMITED, None
    try:
        vk = _read_vk()
    except Exception as e:
        _note_read(READ_ERROR, e)
        return READ_ERROR, None
    if not vk:
        _note_read(NOT_CONFIGURED)
        return NOT_CONFIGURED, None
    _note_read(ENFORCED)
    return ENFORCED, vk


def should_enforce() -> bool:
    """True if we should attach the VK header and respect Bifrost's gating.

    False under DEV_MODE or LLM_BUDGET_UNLIMITED, when no default VK is
    configured (bootstrap window), or when the settings can't be read.
    """
    return enforcement_status()[0] == ENFORCED


# ---------------------------------------------------------------------------
# VK config lookup
# ---------------------------------------------------------------------------


def _read_vk() -> Optional[str]:
    """The configured VK, None if unset. Raises if the settings can't be read."""
    settings = _get_settings()
    if not isinstance(settings, dict):
        return None
    vk = settings.get("default_vk")
    if not isinstance(vk, str) or not vk.strip():
        return None
    return vk.strip()


def get_active_vk() -> Optional[str]:
    """Return the configured global VK ID, or None if not set.

    Reads ``system_config['bifrost.virtual_keys']``. Returns None on any DB
    error so a misconfigured persistence layer can never block LLM traffic;
    the failure is logged as READ_ERROR, distinct from "not configured".
    """
    try:
        vk = _read_vk()
    except Exception as e:
        _note_read(READ_ERROR, e)
        return None
    _note_read(ENFORCED if vk else NOT_CONFIGURED)
    return vk


def get_settings() -> dict:
    """Public read of the full settings dict (for the Budgets UI)."""
    try:
        val = _get_settings() or {}
    except Exception as e:
        logger.debug("budget_service: settings read failed: %s", e)
        return {
            "default_vk": "",
            "budget_limit_usd": 0.0,
            "enforcement_mode": "warning",
        }
    if not isinstance(val, dict):
        return {}
    return {
        "default_vk": val.get("default_vk", "") or "",
        "budget_limit_usd": float(val.get("budget_limit_usd") or 0),
        "enforcement_mode": str(val.get("enforcement_mode") or "warning"),
    }


def set_settings(
    *,
    default_vk: str,
    budget_limit_usd: float,
    enforcement_mode: str,
    updated_by: str,
) -> dict:
    """Persist the VK config. Caller (API handler) is admin-gated.

    ``updated_by`` is the signed-in user, stored on the audit row.
    ``budget_limit_usd`` and ``enforcement_mode`` are stored for
    compatibility. ``should_enforce`` does not read them.
    """
    if enforcement_mode not in ("warning", "hard_stop"):
        raise ValueError(
            f"enforcement_mode must be 'warning' or 'hard_stop', got {enforcement_mode!r}"
        )
    try:
        from core.storage.config_service import get_config_service

        get_config_service(user_id=updated_by).set_system_config(
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
    """Stored settings dict, or None when no row exists. Raises on DB errors."""
    with get_session() as session:
        row = session.query(SystemConfig).filter_by(key=GLOBAL_KEY).first()
        return row.value if row else None
