"""The collaboration service routes session lifecycle through unit_of_work."""

from unittest.mock import MagicMock

import pytest

import core.storage.unit_of_work as uow_module
from core.cases.case_collaboration_service import CaseCollaborationService

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    # __init__ builds a CaseNotificationService; the methods under test here
    # (watchers/reads) don't use it, so a bare instance is enough.
    return CaseCollaborationService()


def test_owned_session_is_committed_and_closed(monkeypatch, service):
    session = MagicMock()
    monkeypatch.setattr(uow_module, "get_db_session", lambda: session)

    assert service.remove_watcher("c1", "u1") is True

    session.delete.assert_called_once()
    session.commit.assert_called_once()
    session.close.assert_called_once()


def test_passed_session_is_not_committed_or_closed(service):
    session = MagicMock()

    assert service.remove_watcher("c1", "u1", session=session) is True

    session.delete.assert_called_once()
    session.commit.assert_not_called()  # the caller's UoW owns the boundary
    session.close.assert_not_called()


def test_read_returns_query_results(service):
    session = MagicMock()
    watchers = [MagicMock(), MagicMock()]
    session.query.return_value.filter.return_value.all.return_value = watchers

    result = service.get_case_watchers("c1", session=session)

    assert result == watchers
    session.close.assert_not_called()
