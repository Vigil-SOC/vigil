"""Runtime-editable AI operations settings (GH #84 PR-F).

The four cost/perf toggles introduced across PR-C and PR-D —
``prompt_cache_enabled``, ``history_window``, ``tool_response_budget_default``,
``thinking_budget`` — need to be adjustable from the Settings UI at runtime
without restarting the backend, daemon, or LLM worker. Persisting them as
env vars in ``.env`` means a restart round-trip per change; persisting
them in ``system_config`` lets us flip values live across all three
processes while still letting operators pin values via env vars in
hardened production deployments.

Resolution order for ``get_ai_operations_setting(key, default)``:
  1. In-process cache (short TTL, default 60s) — avoids per-call DB hits
     in the hot path of ClaudeService/AgentRunner.
  2. ``SystemConfig`` row at key ``ai_operations.settings`` — the live
     source of truth; the Settings UI writes here.
  3. Uppercase-snake env var (``HISTORY_WINDOW`` → ``CLAUDE_HISTORY_WINDOW``)
     — preserves the pre-PR-F behavior when the DB row is absent.
  4. The hard-coded ``default`` passed by the caller.

Tests should prefer ``clear_cache()`` + ``monkeypatch.setenv(...)`` rather
than the cross-process DB round-trip.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CONFIG_KEY = "ai_operations.settings"
_CACHE_TTL_SECONDS = 60

# Each setting maps (Settings-UI field name) -> (fallback env var name).
# Keep this table in sync with ``AIOperationsSettingsConfig`` in
# ``backend/api/config.py`` and with the env-var docs in ``env.example``.
ENV_FALLBACKS = {
    "prompt_cache_enabled": "ANTHROPIC_PROMPT_CACHE_ENABLED",
    "history_window": "CLAUDE_HISTORY_WINDOW",
    "tool_response_budget_default": "TOOL_RESPONSE_BUDGET_DEFAULT",
    "thinking_budget": "CLAUDE_THINKING_BUDGET",
}

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {}
_cache_expires_at: float = 0.0


def clear_cache() -> None:
    """Drop the in-process cache. Called after POST /config/ai-operations
    and from tests that mutate env vars."""
    global _cache, _cache_expires_at
    with _cache_lock:
        _cache = {}
        _cache_expires_at = 0.0


def _fetch_db_config() -> Optional[Dict[str, Any]]:
    """Return the DB-backed config dict, or None if DB unavailable."""
    try:
        from database.config_service import get_config_service
    except Exception as exc:  # noqa: BLE001
        logger.debug("runtime_config: config_service import failed: %s", exc)
        return None
    try:
        svc = get_config_service()
        return svc.get_system_config(_CONFIG_KEY) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("runtime_config: DB fetch failed: %s", exc)
        return None


def _load_cache() -> Dict[str, Any]:
    """Refresh and return the cache. Holds the lock for the read + swap."""
    global _cache, _cache_expires_at
    with _cache_lock:
        if time.monotonic() < _cache_expires_at and _cache:
            return _cache
        fresh = _fetch_db_config() or {}
        _cache = fresh
        _cache_expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        return _cache


def _coerce(value: Any, default: Any) -> Any:
    """Best-effort type match against ``default``.

    Pydantic validates at the write path so DB values should already be
    well-typed, but env-var strings need explicit coercion. Never raise —
    fall back to ``default`` on conversion failure.
    """
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def get_ai_operations_setting(key: str, default: Any) -> Any:
    """Resolve one AI-operations setting (DB > env > default).

    Prefer this over direct ``os.getenv(...)`` in ClaudeService / AgentRunner
    so Settings-UI changes take effect without a restart.
    """
    cache = _load_cache()
    if key in cache:
        return _coerce(cache[key], default)
    env_name = ENV_FALLBACKS.get(key)
    if env_name:
        raw = os.getenv(env_name)
        if raw is not None and raw != "":
            return _coerce(raw, default)
    return default
