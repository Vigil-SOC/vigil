"""Notification metadata must reach the column, not a shadowing instance attribute.

``CaseNotificationService.create_notification`` constructed ``CaseNotification``
with ``metadata=...``, but the column is ``notification_metadata`` — renamed to
dodge SQLAlchemy's reserved name. The call was never updated.

It does not raise. SQLAlchemy's declarative constructor rejects a kwarg only when
``hasattr(type(self), key)`` is false, and ``CaseNotification.metadata`` exists as
the declarative ``MetaData`` inherited from ``Base``. So ``setattr`` succeeds,
creates an instance attribute that shadows the class attribute, and the payload
never reaches a column: it commits cleanly and lands nowhere.

These tests need no database — they assert the payload is on the mapped attribute
before anything is flushed, so they run in the main PR gate. See #559.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DEV_MODE", "true")

REPO = Path(__file__).resolve().parent.parent.parent
for p in (str(REPO), str(REPO / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _FakeCase:
    def __init__(self, case_id, assignee=None):
        self.case_id = case_id
        self.title = "a case"
        self.assignee = assignee


class _FakeQuery:
    def __init__(self, first_result=None, all_result=()):
        self._first = first_result
        self._all = all_result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._first

    def all(self):
        return list(self._all)


class _FakeSession:
    """Captures what would have been written, without a database."""

    def __init__(self, case=None, watchers=()):
        self.added = []
        self.committed = 0
        self._case = case
        self._watchers = watchers

    def query(self, model):
        # notify_sla_warning queries Case (.first()) and then fans out to
        # watchers (.all()); one double has to serve both.
        if getattr(model, "__name__", "") == "CaseWatcher":
            return _FakeQuery(all_result=self._watchers)
        return _FakeQuery(first_result=self._case)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _service():
    from services.case_notification_service import CaseNotificationService

    return CaseNotificationService()


# --------------------------------------------------------------------------
# The payload must land on the mapped column
# --------------------------------------------------------------------------


def test_create_notification_puts_metadata_on_the_column():
    session = _FakeSession()

    notification = _service().create_notification(
        user_id="analyst-1",
        notification_type="sla_warning",
        title="t",
        message="m",
        metadata={"threshold_percent": 90, "sla_type": "response"},
        session=session,
    )

    assert notification is not None, "create_notification returned None"
    assert notification.notification_metadata == {
        "threshold_percent": 90,
        "sla_type": "response",
    }


def test_metadata_is_not_stashed_on_a_shadowing_instance_attribute():
    """The precise failure mode: ``metadata`` set on the instance, column left None.

    Asserting only the column is not enough — a future refactor could set both and
    still be wrong. ``metadata`` on the instance must stay the class-level
    ``MetaData``, never a payload.
    """
    from sqlalchemy import MetaData

    session = _FakeSession()

    notification = _service().create_notification(
        user_id="analyst-1",
        notification_type="sla_warning",
        title="t",
        message="m",
        metadata={"threshold_percent": 90},
        session=session,
    )

    assert "metadata" not in notification.__dict__, (
        "payload was set as an instance attribute shadowing the declarative "
        "MetaData — this is the #559 bug"
    )
    assert isinstance(notification.metadata, MetaData)


def test_absent_metadata_does_not_raise():
    """Three of four call sites pass a payload; stale_case passes none."""
    session = _FakeSession()

    notification = _service().create_notification(
        user_id="analyst-1",
        notification_type="stale_case",
        title="t",
        message="m",
        session=session,
    )

    assert notification is not None
    assert notification.notification_metadata == {}


# --------------------------------------------------------------------------
# The three call sites that pass a literal payload
# --------------------------------------------------------------------------


def test_case_assignment_records_assigned_by():
    session = _FakeSession(case=_FakeCase("CASE-1"))

    assert _service().notify_case_assignment(
        case_id="CASE-1", assignee="analyst-1", assigned_by="lead-1", session=session
    )

    assert len(session.added) == 1
    assert session.added[0].notification_metadata == {"assigned_by": "lead-1"}


def test_comment_mention_records_author_and_content():
    session = _FakeSession(case=_FakeCase("CASE-1"))

    assert _service().notify_comment_mention(
        case_id="CASE-1",
        mentioned_user="analyst-1",
        comment_author="analyst-2",
        comment_content="take a look",
        session=session,
    )

    assert len(session.added) == 1
    assert session.added[0].notification_metadata == {
        "comment_author": "analyst-2",
        "comment_content": "take a look",
    }


def test_sla_warning_records_threshold_and_type():
    # The assignee notification is the one carrying metadata; it is only created
    # when the case has an assignee. No watchers, so nothing is added by the
    # fan-out and the direct notification is the only row.
    session = _FakeSession(case=_FakeCase("CASE-1", assignee="analyst-1"))

    assert _service().notify_sla_warning(
        case_id="CASE-1", threshold_percent=90, sla_type="response", session=session
    )

    assert len(session.added) == 1
    assert session.added[0].notification_metadata == {
        "threshold_percent": 90,
        "sla_type": "response",
    }


# --------------------------------------------------------------------------
# Regression guard: the mistake is invisible, so pin it statically
# --------------------------------------------------------------------------


def test_no_source_file_constructs_casenotification_with_metadata_kwarg():
    """SQLAlchemy accepts ``metadata=`` on any model without complaint.

    Nothing at runtime will ever flag this, so the only durable guard is to look
    at the source. Scans first-party code for ``CaseNotification(...)`` calls
    passing ``metadata=`` instead of ``notification_metadata=``.
    """
    offenders = []
    for directory in ("services", "backend", "daemon", "database"):
        for path in (REPO / directory).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "CaseNotification":
                    continue
                if any(kw.arg == "metadata" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert not offenders, (
        "CaseNotification(...) called with metadata=; the column is "
        f"notification_metadata (see #559): {offenders}"
    )
