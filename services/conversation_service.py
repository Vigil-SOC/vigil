"""Conversation history service — durable, per-analyst chat conversations.

Backs the cross-device chat history for the redesign chat console. A
``Conversation``'s id IS the frontend ``session_id``, so reopening a
conversation lets the in-process ``SessionManager`` restore live context and
continue the same session. This store is separate from
``llm_interaction_logs`` (the compliance audit log), which stays the
system-of-record and is never touched here.

DB access is synchronous via ``get_db_manager().session_scope()``, matching
the ``LLMInteractionLog`` writer in ``claude_service``. The two write paths
invoked from the chat stream — :func:`ensure_conversation` and
:func:`append_message` — are **fail-open**: persistence failures are logged,
never raised, so they can never break the chat. Async callers wrap these in
``asyncio.to_thread(...)``. The user-initiated CRUD helpers (list/get/rename/
archive/delete/import) propagate hard DB errors so the API can surface them,
but return ``None``/``False`` for not-found-or-not-owned.
"""

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, select

from database.connection import get_db_manager
from database.models import ChatMessage, Conversation

logger = logging.getLogger(__name__)

# Title is derived from the first user message, trimmed to keep the list tidy.
_TITLE_MAX = 60


def _derive_title(text: Optional[str]) -> Optional[str]:
    """First user message collapsed to a single line, truncated for the list."""
    if not text:
        return None
    collapsed = " ".join(text.strip().split())
    if not collapsed:
        return None
    if len(collapsed) > _TITLE_MAX:
        return collapsed[:_TITLE_MAX].rstrip() + "…"
    return collapsed


def _owned(session, conversation_id: str, user_id: Optional[str]):
    """Fetch a conversation only if it belongs to ``user_id`` (else ``None``)."""
    return (
        session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        .scalars()
        .first()
    )


# --------------------------------------------------------------------------- #
# Write paths invoked from the chat stream — fail-open (never raise).
# --------------------------------------------------------------------------- #
def ensure_conversation(
    session_id: str,
    user_id: Optional[str],
    agent_id: Optional[str] = None,
    model: Optional[str] = None,
    first_user_text: Optional[str] = None,
) -> Optional[str]:
    """Upsert the conversation row for ``session_id``; return its id or None.

    On create, the title is derived from ``first_user_text`` and ``user_id`` /
    ``agent_id`` are stamped. On an existing row, only ``model`` / ``agent_id``
    are refreshed (never the title — the user may have renamed it). Fail-open.
    """
    try:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            conv = (
                session.execute(
                    select(Conversation).where(Conversation.id == session_id)
                )
                .scalars()
                .first()
            )
            if conv is None:
                conv = Conversation(
                    id=session_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    model=model,
                    title=_derive_title(first_user_text),
                )
                session.add(conv)
            else:
                if model:
                    conv.model = model
                if agent_id:
                    conv.agent_id = agent_id
                if not conv.title and first_user_text:
                    conv.title = _derive_title(first_user_text)
        return session_id
    except Exception as exc:  # noqa: BLE001 — fail-open, must not break chat
        logger.warning("ensure_conversation failed (non-fatal): %s", exc)
        return None


def append_message(
    session_id: str,
    role: str,
    content: str,
    thinking: Optional[str] = None,
    tool_calls: Optional[list] = None,
    complete: bool = True,
    model: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> Optional[int]:
    """Append a message to a conversation; return its row id or None.

    Computes the next dense ``seq`` from the current max, inserts the message,
    and bumps the parent's ``message_count`` / ``updated_at`` /
    ``last_message_at`` (and ``model`` if given). Fail-open. The conversation
    is expected to exist (call :func:`ensure_conversation` first); if it does
    not, the insert is skipped rather than orphaning a message.
    """
    try:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            conv = (
                session.execute(
                    select(Conversation).where(Conversation.id == session_id)
                )
                .scalars()
                .first()
            )
            if conv is None:
                logger.warning(
                    "append_message: no conversation %s; skipping", session_id
                )
                return None

            next_seq = session.execute(
                select(func.coalesce(func.max(ChatMessage.seq), -1)).where(
                    ChatMessage.conversation_id == session_id
                )
            ).scalar_one()
            next_seq = int(next_seq) + 1

            msg = ChatMessage(
                conversation_id=session_id,
                seq=next_seq,
                role=role,
                content=content or "",
                thinking=thinking or None,
                tool_calls=tool_calls or [],
                complete=complete,
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cost_usd=float(cost_usd or 0.0),
            )
            session.add(msg)

            conv.message_count = int(conv.message_count or 0) + 1
            conv.last_message_at = datetime.utcnow()
            if model:
                conv.model = model
            session.flush()
            return msg.id
    except Exception as exc:  # noqa: BLE001 — fail-open, must not break chat
        logger.warning("append_message failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# User-initiated CRUD — propagate hard errors; None/False for not-found.
# --------------------------------------------------------------------------- #
def list_conversations(
    user_id: Optional[str],
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> List[dict]:
    """Conversations for ``user_id``, newest activity first (summary dicts)."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Conversation.archived.is_(False))
        # Coalesce so conversations with no messages yet still sort sanely.
        stmt = stmt.order_by(
            func.coalesce(
                Conversation.last_message_at,
                Conversation.updated_at,
                Conversation.created_at,
            ).desc()
        ).limit(max(1, min(limit, 200)))
        if offset > 0:
            stmt = stmt.offset(offset)
        rows = session.execute(stmt).scalars().all()
        return [c.to_summary_dict() for c in rows]


def get_conversation(conversation_id: str, user_id: Optional[str]) -> Optional[dict]:
    """Conversation + ordered messages, or None if not found / not owned."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        return conv.to_dict()


def rename(
    conversation_id: str, user_id: Optional[str], title: str
) -> Optional[dict]:
    """Set a conversation's title; return its summary dict or None."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        conv.title = (title or "").strip()[:200] or None
        session.flush()
        return conv.to_summary_dict()


def set_archived(
    conversation_id: str, user_id: Optional[str], archived: bool
) -> Optional[dict]:
    """Soft-archive / unarchive a conversation; return summary dict or None."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        conv.archived = bool(archived)
        session.flush()
        return conv.to_summary_dict()


def delete(conversation_id: str, user_id: Optional[str]) -> bool:
    """Hard-delete a conversation (messages cascade); True if removed."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return False
        session.delete(conv)  # ORM cascade removes messages
        return True


def bulk_import(user_id: Optional[str], conversations: List[dict]) -> dict:
    """One-time best-effort import of localStorage history.

    Idempotent: conversations whose id already exists are skipped. Each input
    item: ``{id, title?, agent_id?, model?, created_at?, messages: [...]}``;
    each message: ``{role, content, thinking?, tool_calls?, model?, ...}``.
    Per-conversation failures are isolated so one bad item can't abort the
    whole import. Returns ``{"imported": n, "skipped": m}``.
    """
    imported = 0
    skipped = 0
    db_manager = get_db_manager()
    for item in conversations or []:
        conv_id = (item or {}).get("id")
        if not conv_id:
            skipped += 1
            continue
        try:
            with db_manager.session_scope() as session:
                exists = (
                    session.execute(
                        select(Conversation.id).where(Conversation.id == conv_id)
                    )
                    .scalars()
                    .first()
                )
                if exists:
                    skipped += 1
                    continue

                msgs = item.get("messages") or []
                first_user_text = next(
                    (
                        m.get("content")
                        for m in msgs
                        if (m or {}).get("role") == "user" and m.get("content")
                    ),
                    None,
                )
                conv = Conversation(
                    id=conv_id,
                    user_id=user_id,
                    title=item.get("title") or _derive_title(first_user_text),
                    agent_id=item.get("agent_id"),
                    model=item.get("model"),
                    message_count=len(msgs),
                )
                session.add(conv)
                last_at = None
                for seq, m in enumerate(msgs):
                    m = m or {}
                    session.add(
                        ChatMessage(
                            conversation_id=conv_id,
                            seq=seq,
                            role=m.get("role") or "user",
                            content=m.get("content") or "",
                            thinking=m.get("thinking") or None,
                            tool_calls=m.get("tool_calls") or [],
                            complete=bool(m.get("complete", True)),
                            model=m.get("model"),
                            input_tokens=int(m.get("input_tokens") or 0),
                            output_tokens=int(m.get("output_tokens") or 0),
                            cost_usd=float(m.get("cost_usd") or 0.0),
                        )
                    )
                    last_at = datetime.utcnow()
                if last_at is not None:
                    conv.last_message_at = last_at
            imported += 1
        except Exception as exc:  # noqa: BLE001 — isolate one bad conversation
            logger.warning("bulk_import skipped %s (non-fatal): %s", conv_id, exc)
            skipped += 1
    return {"imported": imported, "skipped": skipped}
