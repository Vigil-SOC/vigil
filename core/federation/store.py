"""DB helpers for federation_sources rows + the global federation toggle.

Kept in a single module so both the daemon runner and the backend API can
use the same code path (no re-implementation drift).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.exceptions import default_on_error
from core.time import utcnow

logger = logging.getLogger(__name__)

GLOBAL_KEY = "federation.settings"


# ---------------------------------------------------------------------------
# Global toggle (system_config.federation.settings)
# ---------------------------------------------------------------------------


def read_global_settings() -> Dict[str, Any]:
    """Like :func:`get_global_settings`, but a store error raises.

    For callers that must tell "federation is off" from "could not read".
    """
    from core.storage.config_service import get_config_service

    cfg = get_config_service().get_system_config(GLOBAL_KEY)
    return cfg if isinstance(cfg, dict) else {"enabled": False}


def get_global_settings() -> Dict[str, Any]:
    """Return the federation.settings JSON, defaulting to ``{"enabled": False}``."""
    try:
        return read_global_settings()
    except Exception as e:
        logger.warning("federation.settings read failed: %s", e)
        return {"enabled": False}


def set_global_settings(value: Dict[str, Any], updated_by: str) -> None:
    """Write ``federation.settings`` (read-modify-write so we don't drop fields).

    ``updated_by`` is the actor stored on the audit row. Callers name one;
    there is no placeholder default.
    """
    try:
        from core.storage.config_service import get_config_service

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


@default_on_error(list, level="warning")
def list_sources() -> List[Dict[str, Any]]:
    """All federation_sources rows as dicts."""
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        rows = session.query(FederationSource).all()
        return FederationSourceSchema.dump_many(rows)


def read_source(source_id: str) -> Optional[Dict[str, Any]]:
    """Like :func:`get_source`, but a store error raises instead of reading as no row."""
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        row = session.get(FederationSource, source_id)
        return FederationSourceSchema.dump(row) if row else None


@default_on_error(None, level="warning")
def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    return read_source(source_id)


@default_on_error(None, level="warning")
def upsert_source(source_id: str, defaults: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ensure a row exists; if missing, insert with ``defaults``.

    Returns the resulting row as dict. Used by the auto-seed step on daemon
    boot — see :func:`core.federation.seed.seed_federation_sources`.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        row = session.get(FederationSource, source_id)
        if row is None:
            row = FederationSource(source_id=source_id, **defaults)
            session.add(row)
            session.flush()
        return FederationSourceSchema.dump(row)


@default_on_error(None, level="warning")
def update_source(source_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Patch arbitrary columns on a source row. Caller validates fields."""
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        row = session.get(FederationSource, source_id)
        if row is None:
            return None
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        session.flush()
        return FederationSourceSchema.dump(row)


def record_success(
    source_id: str,
    *,
    cursor: Dict[str, Any],
    when: Optional[datetime] = None,
    dropped: int = 0,
) -> None:
    """Advance the cursor and add this tick's ``dropped`` to the running total."""
    when = when or utcnow()
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import FederationSource

        with get_db_manager().session_scope() as session:
            # Row lock so concurrent writers add to the total instead of racing it.
            row = (
                session.query(FederationSource)
                .filter_by(source_id=source_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                return
            row.last_poll_at = when
            row.last_success_at = when
            row.last_error = None
            row.consecutive_errors = 0
            row.cursor = cursor or {}
            row.dropped_total = (row.dropped_total or 0) + max(dropped, 0)
    except Exception as e:
        logger.warning("record_success(%s) failed: %s", source_id, e)


def record_failure(source_id: str, error: str) -> None:
    """Increment consecutive_errors. We never auto-disable (per design)."""
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            if row is None:
                return
            row.last_poll_at = utcnow()
            row.last_error = (error or "")[:2000]
            row.consecutive_errors = (row.consecutive_errors or 0) + 1
    except Exception as e:
        logger.warning("record_failure(%s) failed: %s", source_id, e)
