"""Analyst notes on an entity (#3605): what a person on the team said about it.

Recall returns them as its ``notes`` kind, so every run that names the entity
opens with them. A note is written or withdrawn and never edited; withdrawing
deletes the row. Both go on the audit trail in the caller's transaction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY as PGArray
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.memory.entity_keys import normalise_key
from core.memory.recall import note_row
from core.memory.recall_contract import ENTITY_KEY_TYPES, bounded_prose

NOTE_WRITTEN = "memory.note.written"
NOTE_WITHDRAWN = "memory.note.withdrawn"

_COLUMNS = "id, entity_key, statement, written_by, concluded_at"

_INSERT = text(f"""
    INSERT INTO episodic_notes (entity_key, statement, written_by)
    VALUES (:entity_key, :statement, :written_by)
    RETURNING {_COLUMNS}
    """)

_DELETE = text(f"DELETE FROM episodic_notes WHERE id = :id RETURNING {_COLUMNS}")

_ALL = text(f"SELECT {_COLUMNS} FROM episodic_notes ORDER BY concluded_at DESC, id ASC")

_BY_KEYS = text(f"""
    SELECT {_COLUMNS} FROM episodic_notes
    WHERE entity_key = ANY(CAST(:keys AS text[]))
    ORDER BY concluded_at DESC, id ASC
    """).bindparams(bindparam("keys", type_=PGArray(SAText)))


class NoteError(ValueError):
    """A note this cannot write; the message is for the person who asked."""


def entity_key_of(raw: str) -> str:
    """``type:value`` normalised, or NoteError naming the types allowed."""
    key = normalise_key(str(raw or ""))
    if key.partition(":")[0] not in ENTITY_KEY_TYPES:
        raise NoteError(
            f"{raw!r} is not type:value with a type in {', '.join(ENTITY_KEY_TYPES)}"
        )
    return key


def list_notes(
    session: Session, entity_keys: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Notes on these keys, or every note, newest first."""
    if entity_keys:
        keys = [entity_key_of(key) for key in entity_keys]
        rows = session.execute(_BY_KEYS, {"keys": keys})
    else:
        rows = session.execute(_ALL)
    return [note_row(row) for row in rows.mappings()]


def write_note(
    session: Session, *, entity_key: str, statement: str, written_by: str
) -> Dict[str, Any]:
    key = entity_key_of(entity_key)
    said = bounded_prose(statement).strip()
    if not said:
        raise NoteError("a note needs a statement")
    row = note_row(
        session.execute(
            _INSERT, {"entity_key": key, "statement": said, "written_by": written_by}
        )
        .mappings()
        .one()
    )
    _record(session, NOTE_WRITTEN, written_by, row)
    return row


def withdraw_note(
    session: Session, note_id: int, *, withdrawn_by: str
) -> Optional[Dict[str, Any]]:
    """Delete the note, or None when there is none with this id."""
    found = session.execute(_DELETE, {"id": note_id}).mappings().one_or_none()
    if found is None:
        return None
    row = note_row(found)
    _record(session, NOTE_WITHDRAWN, withdrawn_by, row)
    return row


def _record(session: Session, action: str, actor: str, row: Dict[str, Any]) -> None:
    trail_events.record(
        session,
        action,
        actor=actor,
        note_id=row["investigation_id"],
        entity_key=row["entity_key"],
        statement=row["statement"],
    )
