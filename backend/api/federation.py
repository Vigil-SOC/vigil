"""Federation API — control + observe federated monitoring sources.

Wraps :mod:`daemon.federation.store` and :func:`daemon.federation.runner.request_poll_now`
so the Settings → Federation UI can read state and toggle sources without
restarting the daemon. The daemon's ``FederationRunner`` re-reads DB rows on
every tick, so PATCHes here propagate within a few seconds.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from daemon.federation import registry as fed_registry
from daemon.federation import store as fed_store
from daemon.federation.runner import request_poll_now

router = APIRouter()
logger = logging.getLogger(__name__)


_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FederationGlobalSettings(BaseModel):
    enabled: bool = False


class FederationSourcePatch(BaseModel):
    """Partial update for a federation source row.

    Every field is optional — clients only send what they want to change.
    Validation that doesn't fit Pydantic (e.g. min_severity enum) is done in
    the handler so the error message can reference the field by name.
    """

    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = Field(default=None, ge=10, le=86400)
    max_items: Optional[int] = Field(default=None, ge=1, le=10000)
    min_severity: Optional[str] = None  # validated against _VALID_SEVERITIES


class FederationSourceView(BaseModel):
    source_id: str
    enabled: bool
    interval_seconds: int
    max_items: int
    min_severity: Optional[str]
    last_poll_at: Optional[str]
    last_success_at: Optional[str]
    last_error: Optional[str]
    consecutive_errors: int
    is_configured: bool
    default_interval_seconds: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enrich_with_adapter(row: Dict[str, Any]) -> Dict[str, Any]:
    """Merge live adapter state (is_configured, default_interval) into a row dict."""
    adapter = fed_registry.get_adapter(row["source_id"])
    if adapter is None:
        row["is_configured"] = False
        row["default_interval_seconds"] = row.get("interval_seconds", 300)
    else:
        try:
            row["is_configured"] = bool(adapter.is_configured())
        except Exception:
            row["is_configured"] = False
        row["default_interval_seconds"] = adapter.default_interval()
    return row


# ---------------------------------------------------------------------------
# Routes — global settings
# ---------------------------------------------------------------------------


@router.get("/settings")
async def get_settings() -> Dict[str, Any]:
    return fed_store.get_global_settings()


@router.put("/settings")
async def put_settings(payload: FederationGlobalSettings) -> Dict[str, Any]:
    fed_store.set_global_settings({"enabled": payload.enabled}, updated_by="api")
    return fed_store.get_global_settings()


# ---------------------------------------------------------------------------
# Routes — per-source
# ---------------------------------------------------------------------------


@router.get("/sources")
async def list_sources() -> Dict[str, Any]:
    """List federation sources.

    Includes adapters that don't yet have a row (so the UI can show "configure
    me" entries without the daemon being up). Adapters whose integration is
    not configured are still listed but flagged ``is_configured=false``.
    """
    rows_by_id = {row["source_id"]: row for row in fed_store.list_sources()}

    out: List[Dict[str, Any]] = []
    for adapter in fed_registry.list_adapters():
        row = rows_by_id.get(adapter.name)
        if row is None:
            # Adapter known to the registry but not yet seeded (e.g., daemon
            # hasn't booted since this integration was configured). Surface a
            # synthetic row so the UI can still toggle it after seeding.
            row = {
                "source_id": adapter.name,
                "enabled": False,
                "interval_seconds": adapter.default_interval(),
                "max_items": 100,
                "min_severity": None,
                "cursor": {},
                "last_poll_at": None,
                "last_success_at": None,
                "last_error": None,
                "consecutive_errors": 0,
            }
        out.append(_enrich_with_adapter(dict(row)))

    return {"sources": out, "global": fed_store.get_global_settings()}


@router.patch("/sources/{source_id}")
async def patch_source(source_id: str, payload: FederationSourcePatch) -> Dict[str, Any]:
    if fed_registry.get_adapter(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")

    fields: Dict[str, Any] = {}
    if payload.enabled is not None:
        fields["enabled"] = payload.enabled
    if payload.interval_seconds is not None:
        fields["interval_seconds"] = payload.interval_seconds
    if payload.max_items is not None:
        fields["max_items"] = payload.max_items
    if payload.min_severity is not None:
        sev = payload.min_severity.lower() if payload.min_severity else None
        if sev and sev not in _VALID_SEVERITIES:
            raise HTTPException(
                status_code=400,
                detail=f"min_severity must be one of {sorted(_VALID_SEVERITIES)} or empty",
            )
        fields["min_severity"] = sev or None

    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided")

    # Auto-create the row on first PATCH so the UI doesn't need a separate
    # "register" endpoint — same shape we'd seed on daemon boot.
    if fed_store.get_source(source_id) is None:
        adapter = fed_registry.get_adapter(source_id)
        seed_defaults = {
            "enabled": False,
            "interval_seconds": adapter.default_interval() if adapter else 300,
            "max_items": 100,
            "min_severity": None,
            "cursor": {},
            "consecutive_errors": 0,
        }
        seed_defaults.update(fields)
        row = fed_store.upsert_source(source_id, seed_defaults)
    else:
        row = fed_store.update_source(source_id, fields)

    if row is None:
        raise HTTPException(status_code=500, detail="Failed to persist source")
    return _enrich_with_adapter(row)


@router.post("/sources/{source_id}/poll-now")
async def poll_now(source_id: str) -> Dict[str, Any]:
    if fed_registry.get_adapter(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    ok = request_poll_now(source_id)
    return {"ok": ok, "source_id": source_id}


@router.get("/health")
async def health() -> Dict[str, Any]:
    """Compact summary for dashboards: per-source last_success + error count."""
    rows = fed_store.list_sources()
    return {
        "global": fed_store.get_global_settings(),
        "sources": [
            {
                "source_id": r["source_id"],
                "enabled": r["enabled"],
                "last_success_at": r.get("last_success_at"),
                "consecutive_errors": r.get("consecutive_errors", 0),
                "last_error": r.get("last_error"),
            }
            for r in rows
        ],
    }
