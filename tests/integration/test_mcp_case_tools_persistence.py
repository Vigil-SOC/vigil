"""End-to-end proof for issue #1169: the vigil MCP case tools commit.

Every case write tool in ``tools/mcp/vigil.py`` serialized its row with a
``to_dict()`` the models never had. The ``AttributeError`` fired inside the
tool's session, so the write rolled back and the tool answered with an error.
These drive the real tool functions against Postgres -- no stubbed service or
session -- and re-read in a fresh session that the rows landed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture(autouse=True)
def _database():
    """Skip only when Postgres is unreachable; fail on anything else."""
    from core.storage.connection import get_db_manager, init_database

    manager = get_db_manager()
    try:
        manager.initialize()
        session = manager.get_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            "requires a local PostgreSQL (docker compose up -d postgres): " f"{exc}"
        )

    init_database()


def _fresh_session():
    from core.storage.connection import get_db_session

    return get_db_session()


@pytest.fixture
def case_id():
    """A real case made through the MCP tool, deleted afterwards (children cascade)."""
    from tools.mcp import vigil

    created = json.loads(vigil.create_case(title="issue-1169 mcp case tools"))
    cid = created.get("case_id") or created.get("case", {}).get("case_id")
    assert cid, f"create_case failed: {created}"
    yield cid

    session = _fresh_session()
    try:
        session.execute(text("DELETE FROM cases WHERE case_id = :id"), {"id": cid})
        session.commit()
    finally:
        session.close()


def test_case_tools_commit_and_serialize(case_id):
    from core.storage.models import Case, CaseClosureInfo, CaseComment, CaseTask
    from tools.mcp import vigil

    comment = json.loads(vigil.add_case_comment(case_id, "lateral movement confirmed"))
    assert comment.get("success"), comment
    assert comment["comment"]["content"] == "lateral movement confirmed"
    assert comment["comment_id"] is not None

    task = json.loads(vigil.add_case_task(case_id, "Analyze sample", priority="high"))
    assert task.get("success"), task
    assert task["task"]["title"] == "Analyze sample"

    # list_tasks reads in its own, already-closed session.
    tasks = json.loads(vigil.get_case_tasks(case_id))
    assert "error" not in tasks, tasks
    assert [t["task_id"] for t in tasks["tasks"]] == [task["task_id"]]

    closed = json.loads(
        vigil.close_case(case_id, "resolved", root_cause="phished credentials")
    )
    assert closed.get("success"), closed
    assert closed["closure"]["case_id"] == case_id
    assert closed["closure"]["closure_category"] == "resolved"
    assert closed["closure"]["root_cause"] == "phished credentials"

    session = _fresh_session()
    try:
        case = session.query(Case).filter(Case.case_id == case_id).one()
        assert case.status == "closed"
        closure = (
            session.query(CaseClosureInfo)
            .filter(CaseClosureInfo.case_id == case_id)
            .one_or_none()
        )
        assert closure is not None and closure.closure_category == "resolved"
        assert (
            session.query(CaseComment).filter(CaseComment.case_id == case_id).count()
            == 1
        )
        assert session.query(CaseTask).filter(CaseTask.case_id == case_id).count() == 1
    finally:
        session.close()
