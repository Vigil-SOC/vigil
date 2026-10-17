"""A database seeded by 04_config_tables.sql starts in Assist (cold start A6).

DB-backed: the seed's own system_config INSERTs run against the throwaway
database, and the approval row is read the way every process reads it,
through the config service. No mocked config store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from core.config import get_settings
from core.response.approval_service import (
    APPROVAL_CONFIG_KEY,
    ActionStatus,
    ActionType,
    ApprovalService,
    Reversibility,
)
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.reference_seed import inserts_by_table

pytestmark = pytest.mark.external_service

SEED = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "database"
    / "init"
    / "04_config_tables.sql"
)
# What 04_config_tables.sql seeded before Assist became the default. A database
# built by an earlier version still has this row.
EARLIER_SEED = text(
    "INSERT INTO system_config (key, value, description, config_type) "
    "VALUES ('approval.force_manual_approval', '{\"enabled\": false}', "
    "'Force manual approval for all actions regardless of confidence score', "
    "'approval') ON CONFLICT (key) DO NOTHING"
)
KEYS = text("SELECT key FROM system_config")


@pytest.fixture
def seeded(monkeypatch):
    """Run the seed's system_config INSERTs; remove only the rows they added."""
    monkeypatch.delenv("DAEMON_FORCE_APPROVAL", raising=False)
    get_settings.cache_clear()
    engine = get_db_manager().engine
    sql = SEED.read_text(encoding="utf-8")
    with engine.begin() as conn:
        before = set(conn.execute(KEYS).scalars())
        for statement in inserts_by_table(sql)["system_config"]:
            conn.execute(text(statement))
    yield engine
    with engine.begin() as conn:
        added = set(conn.execute(KEYS).scalars()) - before
        if added:
            conn.execute(
                text("DELETE FROM system_config WHERE key = ANY(:keys)"),
                {"keys": sorted(added)},
            )
    get_settings.cache_clear()


def _daemon_action():
    return ApprovalService().create_action(
        action_type=ActionType.BLOCK_IP,
        title="block_ip: 198.51.100.9",
        description="assist by default",
        target="198.51.100.9",
        confidence=0.99,
        reason="test",
        evidence=["ev-1"],
        created_by="daemon",
        reversibility=Reversibility.REVERSIBLE,
    )


def test_the_seed_stores_no_approval_row_and_a_confident_action_waits(seeded):
    assert get_config_service().read_system_config(APPROVAL_CONFIG_KEY) is None
    action = _daemon_action()
    assert action.status == ActionStatus.PENDING.value
    assert action.reason == "test; approval.force_manual_approval=True"


def test_a_row_an_earlier_seed_left_keeps_that_database_in_act(seeded):
    with seeded.begin() as conn:
        conn.execute(EARLIER_SEED)
    action = _daemon_action()
    assert action.status == ActionStatus.APPROVED.value
