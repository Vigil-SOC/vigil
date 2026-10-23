"""Conversation history service — durable, per-analyst chat conversations.

Backs the cross-device chat history for the console chat dock. A
``Conversation``'s id IS the frontend ``session_id``, so reopening a
conversation continues the same session. The text lives in ``conversations``
and ``chat_messages``. Tool calls, results, and cost live in the agent
ledger (``agent_events``), under the run id for this conversation's
generation.

DB access is synchronous via ``get_db_manager().session_scope()``. The two
write paths invoked from the chat stream — :func:`ensure_conversation` and
:func:`append_message` — are **fail-open**: persistence failures are logged,
never raised, so they can never break the chat. Async callers wrap these in
``asyncio.to_thread(...)``. The user-initiated CRUD helpers (list/get/rename/
archive/delete) propagate hard DB errors so the API can surface them,
but return ``None``/``False`` for not-found-or-not-owned.
"""

import logging
from typing import List, Optional

from sqlalchemy import delete as delete_rows
from sqlalchemy import func, or_, select

from core.storage.connection import get_db_manager
from core.storage.models import ChatMessage, Conversation
from core.storage.schemas import ConversationSchema, ConversationSummarySchema
from core.time import utcnow

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
    """Fetch a live conversation only if it belongs to ``user_id`` (else ``None``)."""
    return (
        session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )


def held_by_another(conversation_id: str, user_id: Optional[str]) -> bool:
    """True when a conversation with this id exists and ``user_id`` does not own it."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = session.get(Conversation, conversation_id)
        return conv is not None and conv.user_id != user_id


# --------------------------------------------------------------------------- #
# Write paths invoked from the chat stream — fail-open (never raise).
# --------------------------------------------------------------------------- #
def _apply_case_and_page(
    conv: Conversation, case_id: Optional[str], page_context: Optional[str]
) -> None:
    """``None`` means the request omitted the field and the column stays.

    ``case_id`` of ``""`` clears the column. ``page_context`` is written only
    while the stored value is still null, so a later page does not overwrite
    the page the conversation was opened from.
    """
    if case_id is not None:
        conv.case_id = case_id.strip() or None
    if page_context and conv.page_context is None:
        conv.page_context = page_context.strip() or None


def ensure_conversation(
    session_id: str,
    user_id: Optional[str],
    agent_id: Optional[str] = None,
    model: Optional[str] = None,
    first_user_text: Optional[str] = None,
    case_id: Optional[str] = None,
    page_context: Optional[str] = None,
) -> Optional[str]:
    """Upsert the conversation row for ``session_id``; return its id or None.

    Returns None, changing nothing, when another user owns the row. On create,
    the title is derived from ``first_user_text`` and ``user_id`` /
    ``agent_id`` are stamped. On an existing row, only ``model`` / ``agent_id``
    are refreshed (never a title the user set). A row hidden by delete is
    shown again, keeping its generation, and titled from ``first_user_text``
    when it has none. ``case_id`` and ``page_context`` follow
    :func:`_apply_case_and_page`. Fail-open.
    """
    try:
        db_manager = get_db_manager()
        with db_manager.session_scope() as session:
            conv = session.get(Conversation, session_id)
            if conv is not None and conv.user_id != user_id:
                return None
            if conv is None:
                conv = Conversation(
                    id=session_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    model=model,
                    title=_derive_title(first_user_text),
                )
                _apply_case_and_page(conv, case_id, page_context)
                session.add(conv)
            else:
                if conv.deleted_at is not None:
                    conv.deleted_at = None
                if model:
                    conv.model = model
                if agent_id:
                    conv.agent_id = agent_id
                if not conv.title and first_user_text:
                    conv.title = _derive_title(first_user_text)
                _apply_case_and_page(conv, case_id, page_context)
        return session_id
    except Exception as exc:  # noqa: BLE001 — fail-open, must not break chat
        logger.error("ensure_conversation failed (non-fatal): %s", exc)
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
            conv.last_message_at = utcnow()
            if model:
                conv.model = model
            session.flush()
            return msg.id
    except Exception as exc:  # noqa: BLE001 — fail-open, must not break chat
        logger.error("append_message failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# User-initiated CRUD — propagate hard errors; None/False for not-found.
# --------------------------------------------------------------------------- #
def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_conversations(
    user_id: Optional[str],
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    q: Optional[str] = None,
    case_id: Optional[str] = None,
) -> List[dict]:
    """Conversations for ``user_id``, newest activity first (summary dicts).

    ``q`` matches title, case id, or page key. ``case_id`` matches the case id
    exactly. Rows owned by anyone else are never included, even when the text
    matches.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        stmt = select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        if case_id is not None:
            stmt = stmt.where(Conversation.case_id == case_id)
        needle = (q or "").strip()
        if needle:
            pattern = _like_pattern(needle)
            stmt = stmt.where(
                or_(
                    Conversation.title.ilike(pattern, escape="\\"),
                    Conversation.case_id.ilike(pattern, escape="\\"),
                    Conversation.page_context.ilike(pattern, escape="\\"),
                )
            )
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
        return [ConversationSummarySchema.dump(c) for c in rows]


def get_conversation(conversation_id: str, user_id: Optional[str]) -> Optional[dict]:
    """Conversation + ordered messages, or None if not found / not owned."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        return ConversationSchema.dump(conv)


def rename(conversation_id: str, user_id: Optional[str], title: str) -> Optional[dict]:
    """Set a conversation's title; return its summary dict or None."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        conv.title = (title or "").strip()[:200] or None
        session.flush()
        return ConversationSummarySchema.dump(conv)


def set_case_id(
    conversation_id: str, user_id: Optional[str], case_id: str
) -> Optional[dict]:
    """Attach or clear a case on a conversation that already exists.

    Does not insert a row: an attach before the first turn is held by the
    client and sent with that turn. ``""`` clears the column.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return None
        conv.case_id = (case_id or "").strip() or None
        session.flush()
        return ConversationSummarySchema.dump(conv)


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
        return ConversationSummarySchema.dump(conv)


def ledger_key(session_id: str) -> str:
    """The string ``run_id_for`` hashes for this conversation.

    Generation 0, including every conversation that already exists, and a
    session with no row yet, stay on ``session_id`` so their tool history
    stays on the run already written. A later life is ``{session_id}:{N}``.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = session.get(Conversation, session_id)
        generation = int(conv.generation or 0) if conv is not None else 0
    if generation > 0:
        return f"{session_id}:{generation}"
    return session_id


def delete(conversation_id: str, user_id: Optional[str]) -> bool:
    """Hide a conversation and drop its messages. True if this user owned it.

    The row stays, with ``deleted_at`` set and ``generation`` incremented, so
    a later turn on this id starts a new ledger run. Ledger rows are left
    as they are.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        conv = _owned(session, conversation_id, user_id)
        if conv is None:
            return False
        session.execute(
            delete_rows(ChatMessage).where(
                ChatMessage.conversation_id == conversation_id
            )
        )
        conv.title = None
        conv.case_id = None
        conv.page_context = None
        conv.archived = False
        conv.deleted_at = utcnow()
        conv.generation = int(conv.generation or 0) + 1
        conv.message_count = 0
        return True
