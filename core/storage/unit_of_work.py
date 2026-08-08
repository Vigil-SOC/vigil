"""Unit-of-work seam for the case data layer.

The ``case_*`` services each hand-roll the same session-management dance::

    should_close_session = session is None
    if session is None:
        session = get_db_session()
    try:
        ...
    finally:
        if should_close_session:
            session.close()

That pattern appears 57 times and only ever *closes* the session — it never
commits or rolls back, so a write's durability depends on scattered inline
``session.commit()`` calls and a failure mid-method leaves partial state.

``unit_of_work`` replaces the dance with one context manager that owns session
lifecycle and gives writes a real transaction boundary.
"""

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy.orm import Session

from core.storage.connection import get_db_session


@contextmanager
def unit_of_work(
    session: Optional[Session] = None,
) -> Generator[Session, None, None]:
    """Provide a transactional scope for a series of case operations.

    Usage::

        with unit_of_work() as session:
            session.add(obj)
            # committed on success, rolled back on exception

    If ``session`` is passed the caller already owns a transaction — we yield it
    unchanged and do **not** commit or close, so a method called with a caller's
    session simply joins that transaction (this preserves the existing
    ``session`` keyword on the case services). If ``session`` is ``None`` we open
    one, commit on success, roll back on any exception, and always close.

    Args:
        session: An existing session to join, or ``None`` to own a new one.

    Yields:
        The active SQLAlchemy session.
    """
    if session is not None:
        # Caller owns the session/transaction; just hand it back.
        yield session
        return

    session = get_db_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
