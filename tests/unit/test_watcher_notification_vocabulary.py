"""The set of notification types a watcher can switch off must be declared.

``case_watchers.notification_preferences`` is read with
``prefs.get(notification_type, True)`` (services/case_notification_service.py),
where ``notification_type`` is a runtime argument. Nothing declared which keys
that lookup honours, so the available switches could only be learned by finding
every ``notify_watchers`` call site by hand — and a caller sending an
unrecognised key got a 200 and no effect, which reads as "suppressed" but is not.

These tests pin the vocabulary, the opt-out default that makes an absent key
mean "send", and the API's refusal of keys it cannot honour. See #553.
"""

from __future__ import annotations

import logging
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
# Test doubles: notify_watchers only needs query(...).filter(...).all()
# --------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)

    def close(self):
        pass


class _Watcher:
    """Stands in for a CaseWatcher row."""

    def __init__(self, user_id, notification_preferences):
        self.user_id = user_id
        self.notification_preferences = notification_preferences


def _service_recording_sends(monkeypatch):
    """A CaseNotificationService that records sends instead of writing them."""
    from services.case_notification_service import CaseNotificationService

    sent = []

    def _record(self, user_id, notification_type, **kwargs):
        sent.append((user_id, notification_type))

    monkeypatch.setattr(CaseNotificationService, "create_notification", _record)
    return CaseNotificationService(), sent


# --------------------------------------------------------------------------
# The vocabulary itself
# --------------------------------------------------------------------------


def test_vocabulary_names_exactly_the_types_that_reach_notify_watchers():
    """Change-detector: the declared set must match the real call sites.

    ``new_comment`` comes from services/case_collaboration_service.py and
    ``sla_warning`` from services/case_notification_service.py. Adding a
    notify_watchers call site without registering its type here should fail,
    because an unregistered type cannot be switched off by a watcher.
    """
    from services.case_notification_service import WATCHER_NOTIFICATION_TYPES

    assert set(WATCHER_NOTIFICATION_TYPES) == {"new_comment", "sla_warning"}


def test_types_notified_directly_are_not_in_the_vocabulary():
    """case_assigned / comment_mention / stale_case bypass watcher preferences.

    They call create_notification directly and never consult the column, so
    listing them would advertise a switch that does nothing.
    """
    from services.case_notification_service import WATCHER_NOTIFICATION_TYPES

    for direct_only in ("case_assigned", "comment_mention", "stale_case"):
        assert direct_only not in WATCHER_NOTIFICATION_TYPES


# --------------------------------------------------------------------------
# Opt-out default: absence means send
# --------------------------------------------------------------------------


def test_absent_key_still_sends(monkeypatch):
    service, sent = _service_recording_sends(monkeypatch)
    session = _FakeSession([_Watcher("analyst-1", {})])

    count = service.notify_watchers(
        case_id="CASE-1",
        notification_type="new_comment",
        title="t",
        message="m",
        session=session,
    )

    assert count == 1
    assert sent == [("analyst-1", "new_comment")]


def test_null_preferences_still_send(monkeypatch):
    """The column is nullable and has no default, so NULL must behave as {}."""
    service, sent = _service_recording_sends(monkeypatch)
    session = _FakeSession([_Watcher("analyst-1", None)])

    count = service.notify_watchers(
        case_id="CASE-1",
        notification_type="new_comment",
        title="t",
        message="m",
        session=session,
    )

    assert count == 1
    assert sent == [("analyst-1", "new_comment")]


def test_false_suppresses_and_true_sends(monkeypatch):
    service, sent = _service_recording_sends(monkeypatch)
    session = _FakeSession(
        [
            _Watcher("opted-out", {"new_comment": False}),
            _Watcher("opted-in", {"new_comment": True}),
        ]
    )

    count = service.notify_watchers(
        case_id="CASE-1",
        notification_type="new_comment",
        title="t",
        message="m",
        session=session,
    )

    assert count == 1
    assert sent == [("opted-in", "new_comment")]


def test_preference_is_per_type(monkeypatch):
    """Switching off one type must not switch off another."""
    service, sent = _service_recording_sends(monkeypatch)
    session = _FakeSession([_Watcher("analyst-1", {"new_comment": False})])

    count = service.notify_watchers(
        case_id="CASE-1",
        notification_type="sla_warning",
        title="t",
        message="m",
        session=session,
    )

    assert count == 1
    assert sent == [("analyst-1", "sla_warning")]


# --------------------------------------------------------------------------
# Unregistered types are loud but not fatal
# --------------------------------------------------------------------------


def test_unregistered_type_warns_but_still_sends(monkeypatch, caplog):
    """Fail open: an unregistered type must keep today's send-anyway behaviour.

    The new_comment call site sits inside add_comment's try/except, which rolls
    back and returns None — raising here would turn a registration slip into a
    silently-dropped comment.
    """
    service, sent = _service_recording_sends(monkeypatch)
    session = _FakeSession([_Watcher("analyst-1", {})])

    with caplog.at_level(logging.WARNING):
        count = service.notify_watchers(
            case_id="CASE-1",
            notification_type="escalation",
            title="t",
            message="m",
            session=session,
        )

    assert count == 1
    assert sent == [("analyst-1", "escalation")]
    assert "escalation" in caplog.text
    assert "WATCHER_NOTIFICATION_TYPES" in caplog.text


def test_registered_type_does_not_warn(monkeypatch, caplog):
    service, _sent = _service_recording_sends(monkeypatch)
    session = _FakeSession([_Watcher("analyst-1", {})])

    with caplog.at_level(logging.WARNING):
        service.notify_watchers(
            case_id="CASE-1",
            notification_type="new_comment",
            title="t",
            message="m",
            session=session,
        )

    assert "WATCHER_NOTIFICATION_TYPES" not in caplog.text


# --------------------------------------------------------------------------
# The API boundary refuses keys it cannot honour
# --------------------------------------------------------------------------


def test_watcher_add_rejects_unknown_notification_type():
    """A caller sending {"case_assigned": False} believes it suppressed something."""
    from pydantic import ValidationError

    from backend.api.cases import WatcherAdd

    with pytest.raises(ValidationError) as exc:
        WatcherAdd(
            user_id="analyst-1",
            notification_preferences={"case_assigned": False},
        )

    message = str(exc.value)
    assert "case_assigned" in message
    # The error must name what *is* accepted, not just what is not.
    assert "new_comment" in message and "sla_warning" in message


def test_watcher_add_accepts_known_notification_types():
    from backend.api.cases import WatcherAdd

    model = WatcherAdd(
        user_id="analyst-1",
        notification_preferences={"new_comment": False, "sla_warning": True},
    )

    assert model.notification_preferences == {
        "new_comment": False,
        "sla_warning": True,
    }


def test_watcher_add_allows_omitted_preferences():
    """The frontend posts {user_id} only — that must stay valid."""
    from backend.api.cases import WatcherAdd

    assert WatcherAdd(user_id="analyst-1").notification_preferences is None


def test_watcher_add_rejects_non_boolean_value():
    from pydantic import ValidationError

    from backend.api.cases import WatcherAdd

    with pytest.raises(ValidationError):
        WatcherAdd(
            user_id="analyst-1",
            notification_preferences={"new_comment": "maybe"},
        )


def test_add_watcher_route_returns_422_for_unknown_type():
    """End-to-end through FastAPI: the refusal must surface as a 422, not a 200."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.cases import router

    app = FastAPI()
    app.include_router(router, prefix="/api/cases")
    client = TestClient(app)

    response = client.post(
        "/api/cases/CASE-1/watchers",
        json={
            "user_id": "analyst-1",
            "notification_preferences": {"case_assigned": False},
        },
    )

    assert response.status_code == 422
    assert "case_assigned" in response.text
