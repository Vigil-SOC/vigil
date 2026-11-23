"""DB helpers for federation_sources rows + the global federation toggle.

Kept in a single module so both the daemon runner and the backend API can
use the same code path (no re-implementation drift).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.exceptions import default_on_error

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

    cfg = get_config_service().read_system_config(GLOBAL_KEY)
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


# The health fields a source row has always served, read from its Integration
# Health record (core/integrations/health.py). The row's own columns are no
# longer written.
_NO_HEALTH = {
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "last_error_kind": None,
    "consecutive_errors": 0,
    "dropped_total": 0,
    "filtered_total": 0,
}


def health_fields(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A health record in the federation row's field names and meanings."""
    from core.storage.schemas.base import _iso_utc

    if not record:
        return dict(_NO_HEALTH)

    def iso(value):
        return _iso_utc(value) if value else None

    return {
        "last_poll_at": iso(record.get("last_attempt_at")),
        "last_success_at": iso(record.get("last_success_at")),
        "last_error": record.get("last_error"),
        "last_error_kind": record.get("last_error_kind"),
        "consecutive_errors": record.get("consecutive_failures") or 0,
        "dropped_total": record.get("dropped_total") or 0,
        "filtered_total": record.get("filtered_total") or 0,
    }


def _with_health(
    rows: List[Dict[str, Any]], records: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    from core.integrations.health import health_id

    return [
        {**row, **health_fields(records.get(health_id(row["source_id"])))}
        for row in rows
    ]


def read_sources(with_health: bool = True) -> List[Dict[str, Any]]:
    """All federation_sources rows as dicts, health from the record; a store error raises."""
    from core.integrations import health
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        rows = FederationSourceSchema.dump_many(session.query(FederationSource).all())
    return _with_health(rows, health.read_all()) if with_health else rows


def read_source(source_id: str) -> Optional[Dict[str, Any]]:
    """Like :func:`get_source`, but a federation_sources error raises instead of
    reading as no row. A failed health read serves the row with no health."""
    from core.integrations import health
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        row = session.get(FederationSource, source_id)
        if row is None:
            return None
        dumped = FederationSourceSchema.dump(row)
    try:
        record = health.read(health.health_id(source_id))
    except Exception as e:  # noqa: BLE001
        # Health is beside collection: the runner keeps polling (no backoff
        # count) and a PATCH that was saved still answers with its row.
        logger.warning("Health read for %s failed: %s", source_id, type(e).__name__)
        record = None
    return {**dumped, **health_fields(record)}


@default_on_error(None, level="warning")
def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    return read_source(source_id)


# The operator-set columns a source change records on the audit trail.
_SETTINGS = ("enabled", "interval_seconds", "max_items", "min_severity")


def _settings(row) -> Dict[str, Any]:
    return {k: getattr(row, k, None) for k in _SETTINGS}


def _audit_source(session, actor, source_id, verb, old, new) -> None:
    """Record a source change in the caller's transaction; a failure rolls it back."""
    from core.storage.config_service import ConfigService

    ConfigService(user_id=actor)._create_audit_log(
        session,
        config_type="federation",
        config_key=f"federation.source:{source_id}",
        action=verb,
        old_value=old,
        new_value=new,
    )


@default_on_error(None, level="warning")
def upsert_source(
    source_id: str, defaults: Dict[str, Any], actor: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Ensure a row exists; if missing, insert with ``defaults``.

    Returns the resulting row as dict. Used by the auto-seed step on daemon
    boot — see :func:`core.federation.seed.seed_federation_sources` — and by
    the API, which names ``actor`` so an insert is recorded on the trail.
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
            if actor:
                _audit_source(session, actor, source_id, "create", None, _settings(row))
        dumped = FederationSourceSchema.dump(row)
    return read_source(source_id) or dumped


@default_on_error(None, level="warning")
def update_source(
    source_id: str, fields: Dict[str, Any], actor: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Patch arbitrary columns on a source row. Caller validates fields.

    With ``actor``, the change is recorded on the trail in the same transaction.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource
    from core.storage.schemas import FederationSourceSchema

    with get_db_manager().session_scope() as session:
        row = session.get(FederationSource, source_id)
        if row is None:
            return None
        old = _settings(row)
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        session.flush()
        if actor:
            _audit_source(session, actor, source_id, "update", old, _settings(row))
        dumped = FederationSourceSchema.dump(row)
    return read_source(source_id) or dumped


def record_success(
    source_id: str,
    *,
    cursor: Dict[str, Any],
    dropped: int = 0,
    received: int = 0,
    returned: int = 0,
    filtered: int = 0,
) -> None:
    """Advance the cursor, then record the poll on the source's health record.

    ``received`` is the acknowledged (stored) count; ``returned`` and
    ``filtered`` feed the record's window counts. Two transactions: a failed
    health write must not hold the cursor back.
    """
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import FederationSource

        with get_db_manager().session_scope() as session:
            row = session.get(FederationSource, source_id)
            if row is None:
                return
            row.cursor = cursor or {}
    except Exception as e:
        logger.warning("record_success(%s) failed: %s", source_id, e)
        return
    from core.integrations import health

    health.record_polled(
        health.health_id(source_id),
        returned=returned,
        stored=received,
        filtered=filtered,
        dropped=dropped,
    )


def record_failure(source_id: str, error: str, kind: str = "unknown") -> None:
    """One more consecutive failure on the health record. We never auto-disable.

    ``kind`` is one of ``error_kind.KINDS``; the runner passes the classifier's answer.
    """
    from core.integrations import health

    health.record_failed(health.health_id(source_id), error or "", kind)
