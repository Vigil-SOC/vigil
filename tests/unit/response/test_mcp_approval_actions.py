"""The MCP approval tools write a row and can list it back (#1392).

Same database stand-in as ``test_approval_workflow``: no PostgreSQL. The
session remembers ``add`` so a later ``list_actions`` sees the insert.
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from core.config import get_settings
from tools.mcp import vigil


@pytest.fixture
def no_db():
    stored = []
    session = MagicMock()

    def add(row):
        stored.append(row)

    def execute(stmt):
        rows = [row for row in stored if _matches(row, stmt)]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    session.add.side_effect = add
    session.execute.side_effect = execute

    manager = MagicMock()

    @contextmanager
    def _scope():
        yield session

    manager.session_scope = _scope
    config_store = Mock()
    config_store.read_system_config.return_value = {"enabled": False}
    with (
        patch("core.response.approval_service.get_db_manager", return_value=manager),
        patch(
            "core.response.approval_service.get_config_service",
            return_value=config_store,
        ),
    ):
        yield


def _matches(row, stmt) -> bool:
    for clause in stmt._where_criteria:
        if getattr(row, clause.left.key) != clause.right.value:
            return False
    return True


@pytest.mark.usefixtures("no_db")
def test_create_approval_action_is_listed_while_pending(monkeypatch):
    monkeypatch.setenv("DAEMON_CONFIDENCE_THRESHOLD", "0.90")
    get_settings.cache_clear()
    try:
        created = json.loads(
            vigil.create_approval_action(
                action_type="block_ip",
                title="block scanner",
                description="repeated probes",
                target="203.0.113.7",
                confidence=0.5,
                reason="scan",
            )
        )
        assert "Service error" not in json.dumps(created)
        assert created["success"] is True
        assert created["status"] == "pending"

        listed = json.loads(vigil.list_approval_actions(status="pending"))
        assert "Service error" not in json.dumps(listed)
        assert listed["success"] is True
        assert listed["count"] == 1
        action = listed["actions"][0]
        assert action["action_id"] == created["action_id"]
        assert action["action_type"] == "block_ip"
        assert action["title"] == "block scanner"
        assert action["target"] == "203.0.113.7"
        assert action["confidence"] == 0.5
        assert action["status"] == "pending"
        assert action["created_at"]
    finally:
        get_settings.cache_clear()
