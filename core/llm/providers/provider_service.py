"""Persistence for LLM provider rows.

Owns every query behind ``/api/llm/providers`` so the router holds the session
without touching it. Nothing here commits — the request's unit of work does —
and nothing raises ``HTTPException``: absence is ``None`` and the router maps
it to a status code.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from sqlalchemy import delete as sa_delete
from sqlalchemy import update
from sqlalchemy.orm import Session

from core.llm.bifrost.admin import (
    credential_row,
    push_provider_key,
    row_credential_view,
)
from core.storage.models import AIModelConfig, LLMProviderConfig

logger = logging.getLogger(__name__)


def list_providers(db: Session) -> List[LLMProviderConfig]:
    return db.query(LLMProviderConfig).order_by(LLMProviderConfig.created_at).all()


def active_provider_configs(db: Session) -> List[Tuple[str, Optional[dict]]]:
    """``(provider_type, config)`` of every active row, for the pricing attestation."""
    return [(r.provider_type, r.config) for r in list_providers(db) if r.is_active]


def get_provider(db: Session, provider_id: str) -> Optional[LLMProviderConfig]:
    return db.get(LLMProviderConfig, provider_id)


def find_other_default(
    db: Session, provider_type: str, exclude_provider_id: str
) -> Optional[LLMProviderConfig]:
    """Another provider of this type already flagged default, if one exists."""
    return (
        db.query(LLMProviderConfig)
        .filter(
            LLMProviderConfig.provider_type == provider_type,
            LLMProviderConfig.provider_id != exclude_provider_id,
            LLMProviderConfig.is_default.is_(True),
        )
        .first()
    )


def find_other_active(
    db: Session, provider_type: str, exclude_provider_id: str
) -> Optional[LLMProviderConfig]:
    """Oldest other active provider of this type — the promotion candidate."""
    return (
        db.query(LLMProviderConfig)
        .filter(
            LLMProviderConfig.provider_type == provider_type,
            LLMProviderConfig.provider_id != exclude_provider_id,
            LLMProviderConfig.is_active.is_(True),
        )
        .order_by(LLMProviderConfig.created_at)
        .first()
    )


def clear_other_defaults(db: Session, provider_type: str, keep_id: str) -> None:
    """Enforce the 'one default per provider_type' invariant at the app layer.

    The DB has a partial unique index (``llm_provider_default_per_type``,
    ``WHERE is_default = TRUE``) too, but clearing first avoids the
    index-conflict round-trip on UPDATE.

    The ``no_autoflush`` guard is load-bearing: callers set ``keep_id``'s
    ``is_default = True`` (or stage an INSERT with it) *before* calling here,
    so the Core UPDATE below would otherwise trigger an autoflush that writes
    the new default while the old one is still TRUE — two defaults at once,
    which the partial unique index rejects with an IntegrityError (500).
    Suppressing autoflush lets the UPDATE clear the old default first; the
    pending ``keep_id`` change then flushes safely at commit.
    """
    with db.no_autoflush:
        db.execute(
            update(LLMProviderConfig)
            .where(
                LLMProviderConfig.provider_type == provider_type,
                LLMProviderConfig.provider_id != keep_id,
                LLMProviderConfig.is_default.is_(True),
            )
            .values(is_default=False)
        )


def reconcile_bifrost_key_for_type(
    db: Session, provider_type: str, exclude_provider_id: Optional[str] = None
) -> None:
    """Re-point Bifrost's per-type key after a keyed row of the type changes.

    Bifrost stores a single key and host per ``provider_type`` (see
    ``bifrost_admin.push_provider_key``), so the type's active keyed rows elect
    one (``credential_row``: the default first) and its key is pushed with its
    own host. An inactive row never counts. With no keyed row left, the
    gateway's key is cleared. ``exclude_provider_id`` is a row being deleted
    or turned off, which is still in the session.
    """
    rows = [
        row_credential_view(r)
        for r in db.query(LLMProviderConfig)
        .filter(
            LLMProviderConfig.provider_type == provider_type,
            LLMProviderConfig.is_active.is_(True),
            LLMProviderConfig.api_key_ref.isnot(None),
        )
        .order_by(LLMProviderConfig.created_at)
        if r.provider_id != exclude_provider_id
    ]
    chosen, value = credential_row(rows)
    if chosen is None:
        push_provider_key(provider_type, "")
    else:
        push_provider_key(provider_type, value or "", chosen.get("base_url"))


def delete_model_configs_for_provider(db: Session, provider_id: str) -> None:
    """Clear AIModelConfig rows referencing this provider.

    The FK is ON DELETE RESTRICT, so this must run before deleting the row.
    """
    db.execute(sa_delete(AIModelConfig).where(AIModelConfig.provider_id == provider_id))


def insert(db: Session, row: LLMProviderConfig, *, make_default: bool) -> None:
    """Stage a new provider, enforcing the one-default-per-type invariant."""
    db.add(row)
    if make_default:
        clear_other_defaults(db, row.provider_type, row.provider_id)
    settle(db, row)


def set_default(db: Session, provider_id: str) -> Optional[LLMProviderConfig]:
    """Promote a provider to default, demoting its same-type siblings."""
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        return None
    row.is_default = True
    clear_other_defaults(db, row.provider_type, provider_id)
    settle(db, row)
    return row


def demote_and_promote(
    db: Session, row: LLMProviderConfig, successor: LLMProviderConfig
) -> None:
    """Hand the default flag to ``successor``.

    The flush between the two writes is load-bearing: the partial unique index
    rejects a moment where both rows are default.
    """
    row.is_default = False
    db.flush()
    successor.is_default = True


def remove(db: Session, row: LLMProviderConfig) -> None:
    db.delete(row)


def settle(db: Session, row: LLMProviderConfig) -> None:
    """Flush pending writes so a read-back sees server-side defaults."""
    db.flush()
    db.refresh(row)
