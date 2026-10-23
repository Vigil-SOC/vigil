"""Who an agent tool call acts as, and the session it writes in."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from core.integrations.mcp.surface import current_caller
from core.storage.connection import get_db_session

# No principal bound: nobody is behind the call, as in a hunt.
CALLER_UNAUTHENTICATED = "agent"


def actor() -> str:
    """Who this call writes into a record. Never an argument the model supplied."""
    return current_caller() or CALLER_UNAUTHENTICATED


@contextmanager
def service_session() -> Iterator:
    """A session of the caller's own, committed if the work returns.

    Tools reach the database directly rather than through the API, so each
    call owns its transaction. One definition, so commit means one thing.
    """
    session = get_db_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()
