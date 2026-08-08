"""Tests for the case-layer unit-of-work seam."""

from unittest.mock import MagicMock

import pytest

import core.storage.unit_of_work as uow_module
from core.storage.unit_of_work import unit_of_work

pytestmark = pytest.mark.unit


def _patch_session(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(uow_module, "get_db_session", lambda: session)
    return session


def test_commits_and_closes_on_success(monkeypatch):
    session = _patch_session(monkeypatch)

    with unit_of_work() as s:
        assert s is session

    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    session.close.assert_called_once()


def test_rolls_back_and_closes_on_error(monkeypatch):
    session = _patch_session(monkeypatch)

    with pytest.raises(ValueError):
        with unit_of_work():
            raise ValueError("boom")

    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_passed_session_is_not_committed_or_closed(monkeypatch):
    # No new session should be created when the caller supplies one.
    monkeypatch.setattr(
        uow_module,
        "get_db_session",
        lambda: pytest.fail("should not open a new session"),
    )
    caller_session = MagicMock()

    with unit_of_work(session=caller_session) as s:
        assert s is caller_session

    caller_session.commit.assert_not_called()
    caller_session.rollback.assert_not_called()
    caller_session.close.assert_not_called()
