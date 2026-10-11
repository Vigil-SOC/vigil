"""Skill usage: the recorder and the 7-day grouped query (#1560).

Run against an in-memory SQLite database standing in for the manager: the
``skill_reads`` model is dialect-portable, and what is pinned here is the
counting — the window, the distinct agents, and the retention prune — not
the database.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.skills import skill_usage
from core.storage.models import SkillRead
from core.time import utcnow

pytestmark = pytest.mark.unit


@pytest.fixture()
def manager(monkeypatch):
    engine = create_engine("sqlite://")
    # SQLite has no bigserial: the same table with an INTEGER rowid key.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE skill_reads ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "skill_name TEXT NOT NULL, "
                "agent_id TEXT, "
                "read_at TIMESTAMP NOT NULL)"
            )
        )
    factory = sessionmaker(bind=engine)

    class _Manager:
        @contextmanager
        def session_scope(self):
            session = factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    stub = _Manager()
    monkeypatch.setattr(skill_usage, "get_db_manager", lambda: stub)
    return stub


def _seed(manager, name, agent_id, days_ago):
    with manager.session_scope() as session:
        session.add(
            SkillRead(
                skill_name=name,
                agent_id=agent_id,
                read_at=utcnow() - timedelta(days=days_ago),
            )
        )


def test_usage_counts_reads_and_distinct_agents_in_the_window(manager):
    _seed(manager, "triage", "agent-1", days_ago=1)
    _seed(manager, "triage", "agent-1", days_ago=2)
    _seed(manager, "triage", "agent-2", days_ago=3)
    _seed(manager, "triage", None, days_ago=1)
    _seed(manager, "triage", "agent-3", days_ago=9)
    _seed(manager, "other", "agent-1", days_ago=1)

    usage = skill_usage.skill_usage()

    # Four reads in the window; the NULL agent raises the count without
    # raising the agent count, and the 9-day-old read is outside it.
    assert usage["triage"] == (4, 2)
    assert usage["other"] == (1, 1)


def test_recording_a_read_prunes_past_retention(manager):
    _seed(manager, "triage", "agent-1", days_ago=9)
    skill_usage.record_skill_read("triage", "agent-2")
    with manager.session_scope() as session:
        rows = [
            (row.skill_name, row.agent_id) for row in session.query(SkillRead).all()
        ]
    assert rows == [("triage", "agent-2")]


def test_recording_an_unattributed_read_keeps_the_agent_null(manager):
    skill_usage.record_skill_read("triage", None)
    assert skill_usage.skill_usage()["triage"] == (1, 0)
