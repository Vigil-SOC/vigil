"""Conversation history API — cross-device, per-analyst chat history.

Surfaces the durable conversation store behind the redesign chat console:
list past conversations, reopen one with its full message history, rename,
soft-archive, hard-delete, and a one-time localStorage import. Every handler
is auth-gated and scoped to the authenticated user via ``get_current_user``.

This store is separate from ``llm_interaction_logs`` (the compliance audit
log), which is never touched here — deleting a conversation does not remove
its audit trail.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.middleware.auth import get_current_user
from database.models import User
from services import conversation_service

logger = logging.getLogger(__name__)

router = APIRouter()


class UpdateConversationRequest(BaseModel):
    """PATCH body — rename and/or archive (either or both)."""

    title: Optional[str] = None
    archived: Optional[bool] = None


class ImportConversationsRequest(BaseModel):
    """POST /import body — best-effort bulk import of localStorage history."""

    conversations: List[dict] = []


@router.get("/")
async def list_conversations(
    archived: bool = Query(False, description="Include archived conversations"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List the current user's conversations, newest activity first."""
    items = conversation_service.list_conversations(
        user_id=current_user.user_id,
        include_archived=archived,
        limit=limit,
        offset=offset,
    )
    return {"conversations": items}


@router.post("/import")
async def import_conversations(
    body: ImportConversationsRequest,
    current_user: User = Depends(get_current_user),
):
    """One-time best-effort import of browser localStorage history.

    Idempotent — conversations whose id already exists are skipped. Returns
    ``{"imported": n, "skipped": m}``.
    """
    return conversation_service.bulk_import(current_user.user_id, body.conversations)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Fetch a single conversation with its ordered messages."""
    conv = conversation_service.get_conversation(
        conversation_id, current_user.user_id
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.patch("/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    current_user: User = Depends(get_current_user),
):
    """Rename and/or archive a conversation."""
    if body.title is None and body.archived is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    result = None
    if body.title is not None:
        result = conversation_service.rename(
            conversation_id, current_user.user_id, body.title
        )
    if body.archived is not None:
        result = conversation_service.set_archived(
            conversation_id, current_user.user_id, body.archived
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Hard-delete a conversation (messages cascade)."""
    ok = conversation_service.delete(conversation_id, current_user.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True, "id": conversation_id}
