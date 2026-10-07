"""Declarative Base, JSONB list type, and association tables."""

from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase

from core.time import utcnow

JSONBList = MutableList.as_mutable(JSONB)


class Base(DeclarativeBase):
    """Base class for all database models.

    Overrides the declarative constructor for one reason: to refuse
    ``metadata=``. SQLAlchemy accepts any kwarg for which ``hasattr(cls, key)``
    holds, and ``metadata`` always holds — every declarative class inherits
    ``Base.metadata``. The value lands on the instance, shadows the
    ``MetaData``, reaches no column, and commits without error. Models that
    need such a column rename it (``notification_metadata``,
    ``decision_metadata``), so a bare ``metadata=`` is always a mistake, and
    the only mistake here that nothing else can see. See #559.
    """

    def __init__(self, **kwargs: Any) -> None:
        cls = type(self)
        for key, value in kwargs.items():
            if key == "metadata":
                raise TypeError(
                    f"{cls.__name__}(metadata=...) shadows the declarative "
                    "MetaData and never reaches a column; pass the renamed "
                    "column instead (e.g. notification_metadata)."
                )
            if not hasattr(cls, key):
                raise TypeError(
                    f"{key!r} is an invalid keyword argument for {cls.__name__}"
                )
            setattr(self, key, value)


# Association table for case-finding many-to-many relationship
case_findings = Table(
    "case_findings",
    Base.metadata,
    Column(
        "case_id",
        String,
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "finding_id",
        String,
        ForeignKey("findings.finding_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("added_at", DateTime, default=utcnow, nullable=False),
    # The primary key leads with case_id, so it cannot serve a lookup by
    # finding: Finding.cases (selectin on every Finding load), the ON DELETE
    # CASCADE from findings, and "which cases hold this finding".
    Index("idx_case_findings_finding", "finding_id", "case_id"),
)
