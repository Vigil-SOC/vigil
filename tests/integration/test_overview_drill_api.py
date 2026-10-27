"""GET /api/overview/outcomes/{state} and /api/overview/agents/{id}: what a click on the board opens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
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
            f"requires a local PostgreSQL (docker compose up -d postgres): {exc}"
        )
    init_database()


@pytest.fixture(scope="module")
def client():
    from services.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _authenticated(client, authenticate_app):
    authenticate_app(client.app)


def test_each_outcome_lists_as_many_alerts_as_the_board_counts(client):
    board = client.get("/api/overview").json()
    for node in board["outcomes"]:
        if node["count"] is None:
            continue
        r = client.get(f"/api/overview/outcomes/{node['state']}", params={"limit": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["label"] == node["label"]
        assert body["total"] == node["count"]
        assert len(body["items"]) == min(5, node["count"])
        assert sum(s["count"] for s in body["by_source"]) == body["total"]
        times = [i["created_at"] for i in body["items"] if i["created_at"]]
        assert times == sorted(times, reverse=True)


def test_an_unknown_outcome_is_404(client):
    assert client.get("/api/overview/outcomes/nope").status_code == 404


def test_an_agent_card_opens_its_workflow_and_record(client):
    board = client.get("/api/overview").json()
    assert board["agents"], "the board lists the workflows"
    wid = board["agents"][0]["workflow_id"]
    body = client.get(f"/api/overview/agents/{wid}").json()
    assert body["workflow"]["id"] == wid
    assert set(body["record"]) >= {"runs", "completed", "failed", "rate", "median_ms"}
    assert isinstance(body["live"], list) and isinstance(body["recent"], list)
    assert client.get("/api/overview/agents/no-such-workflow").status_code == 404


def test_an_agent_record_matches_its_card_with_hidden_runs(client):
    from datetime import timedelta

    from core.storage.connection import get_db_manager
    from core.storage.models import WorkflowRun
    from core.time import utcnow

    board = client.get("/api/overview").json()
    wid = board["agents"][0]["workflow_id"]
    now = utcnow().replace(tzinfo=None)
    ids = ["drill-hidden-1", "drill-hidden-2"]
    with get_db_manager().session_scope() as session:
        session.add_all(
            [
                WorkflowRun(
                    run_id=ids[0],
                    workflow_id=wid,
                    workflow_name=wid,
                    status="completed",
                    started_at=now - timedelta(hours=1),
                ),
                WorkflowRun(
                    run_id=ids[1],
                    workflow_id=wid,
                    workflow_name=wid,
                    status="failed",
                    started_at=now - timedelta(hours=2),
                    deleted_at=now,
                ),
            ]
        )
    try:
        card = next(
            a
            for a in client.get("/api/overview").json()["agents"]
            if a["workflow_id"] == wid
        )
        record = client.get(f"/api/overview/agents/{wid}").json()["record"]
        assert record["runs"] == card["sample_size"]
        assert record["rate"] == pytest.approx(card["rate"])
    finally:
        with get_db_manager().session_scope() as session:
            session.query(WorkflowRun).filter(WorkflowRun.run_id.in_(ids)).delete(
                synchronize_session=False
            )
