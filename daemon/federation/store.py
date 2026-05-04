"""DB helpers for federation_sources rows + the global federation toggle.

Kept in a single module so both the daemon runner and the backend API can
use the same code path (no re-implementation drift).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GLOBAL_KEY = "federation.settings"


# ---------------------------------------------------------------------------
# Global toggle (system_config.federation.settings)
# ---------------------------------------------------------------------------


def get_global_settings() -> Dict[str, Any]:
    """Return the federation.settings JSON, defaulting to ``{"enabled": False}``."""
    try:
        from database.config_service import get_config_service

        cfg = get_config_service().get_system_config(GLOBAL_KEY)
        if isinstance(cfg, dict):
            return cfg
    except Exception as e:
        logger.debug("federation.settings read failed: %s", e)
    return {"enabled": False}


def set_global_settings(value: Dict[str, Any], updated_by: str = "api") -> None:
    """Write ``federation.settings`` (read-modify-write so we don't drop fields)."""
    try:
        from database.config_service import get_config_service

        current = get_global_settings()
        current.update(value)
        get_config_service(user_id=updated_by).set_system_config(
            key=GLOBAL_KEY,
            value=current,
            description="Federated monitoring global on/off",
            config_type="federation",
        )
    except Exception as e:
        logger.error("federation.settings write failed: %s", e)
        raise


def is_globally_enabled() -> bool:
    return bool(get_global_settings().get("enabled", False))


# ---------------------------------------------------------------------------
# Per-source row helpers (federation_sources table)
# ---------------------------------------------------------------------------


def list_sources() -> List[Dict[str, Any]]:
    """All federation_sources rows as dicts."""
    try:
        from database.connection import get_db_manager
        from database.models import FederationSource

        with get_db_manager().session_scope() as session:
            rows = session.query(FederationSource).all()
            return [r.to_dict() for r in rows]
    except Exception as e:
        logger.debug("list federation_sources failed: %s", e)
        return []


def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    try:
        from database.connection import get_db_manager
        from database.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            return row.to_dict() if row else None
    except Exception as e:
        logger.debug("get federation_source(%s) failed: %s", source_id, e)
        return None


def upsert_source(source_id: str, defaults: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ensure a row exists; if missing, insert with ``defaults``.

    Returns the resulting row as dict. Used by the auto-seed step on daemon
    boot — see :func:`daemon.federation.seed.seed_federation_sources`.
    """
    try:
        from database.connection import get_db_manager
        from database.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            if row is None:
                row = FederationSource(source_id=source_id, **defaults)
                session.add(row)
                session.flush()
            return row.to_dict()
    except Exception as e:
        logger.warning("upsert federation_source(%s) failed: %s", source_id, e)
        return None


def update_source(source_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Patch arbitrary columns on a source row. Caller validates fields."""
    try:
        from database.connection import get_db_manager
        from database.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            if row is None:
                return None
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            session.flush()
            return row.to_dict()
    except Exception as e:
        logger.warning("update federation_source(%s) failed: %s", source_id, e)
        return None


def record_success(
    source_id: str, *, cursor: Dict[str, Any], when: Optional[datetime] = None
) -> None:
    when = when or datetime.utcnow()
    update_source(
        source_id,
        {
            "last_poll_at": when,
            "last_success_at": when,
            "last_error": None,
            "consecutive_errors": 0,
            "cursor": cursor or {},
        },
    )


def record_failure(source_id: str, error: str) -> None:
    """Increment consecutive_errors. We never auto-disable (per design)."""
    try:
        from database.connection import get_db_manager
        from database.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            if row is None:
                return
            row.last_poll_at = datetime.utcnow()
            row.last_error = (error or "")[:2000]
            row.consecutive_errors = (row.consecutive_errors or 0) + 1
    except Exception as e:
        logger.debug("record_failure(%s) failed: %s", source_id, e)
