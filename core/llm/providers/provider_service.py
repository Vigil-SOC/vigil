"""Persistence for LLM provider rows.

Owns every query behind ``/api/llm/providers`` so the router holds the session
without touching it. Nothing here commits — the request's unit of work does —
and nothing raises ``HTTPException``: absence is ``None`` and the router maps
it to a status code.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import update
from sqlalchemy.orm import Session

from core.llm.bifrost.admin import push_provider_key
from core.secrets import get_secret
from core.storage.models import AIModelConfig, LLMProviderConfig

logger = logging.getLogger(__name__)


def list_providers(db: Session) -> List[LLMProviderConfig]:
    return db.query(LLMProviderConfig).order_by(LLMProviderConfig.created_at).all()


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
    db: Session, provider_type: str, exclude_provider_id: str
) -> None:
    """Reconcile Bifrost's shared per-type key after a provider loses its key.

    Bifrost stores a single key per ``provider_type`` (see
    ``bifrost_admin.push_provider_key`` — keyed by type, not by row), so
    blindly blanking that key whenever one provider of the type is deleted
    or cleared would knock out every same-type sibling that still relies on
    it (the catalog resync only re-syncs the model allow-list, not the keys,
    so the type would stay keyless until a restart).

    So: only blank Bifrost's key when this was the last provider of its type
    with a key; otherwise re-push a surviving sibling's key.
    ``exclude_provider_id`` is the row being deleted/cleared, so it never
    counts as a survivor.
    """
    survivors = [
        r
        for r in db.query(LLMProviderConfig).all()
        if r.provider_type == provider_type
        and r.provider_id != exclude_provider_id
        and r.api_key_ref
    ]
    # Prefer the default provider, then any active one, for a stable choice.
    survivors.sort(key=lambda r: (not r.is_default, not r.is_active))
    for survivor in survivors:
        value = get_secret(survivor.api_key_ref)
        if value:
            push_provider_key(provider_type, value)
            return
    # No surviving provider of this type has a usable key — it was the last
    # one, so clear the stale credential on Bifrost.
    push_provider_key(provider_type, "")


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
