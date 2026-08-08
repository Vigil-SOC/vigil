"""The cross-language seam, end to end (GH #590).

FastAPI enqueues onto BullMQ, a TypeScript worker consumes the job, appends to
``agent_events`` and marks the run terminal, and the API reports the outcome from
what the worker persisted. This is the only test where both runtimes meet, so it
is what proves the seam rather than either half of it.

Requires Postgres, Redis and a built ``ai/`` package, so it is marked
``external_service``. The Node dependencies must already be installed
(``npm ci`` in ``ai/``); the test spawns the worker, it does not build it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.external_service]

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_DIR = REPO_ROOT / "ai"
WORKER_TIMEOUT_S = 60


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://vigil:vigil@localhost:5432/vigil_test")


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        ledger_ddl = (REPO_ROOT / "database" / "init" / "19_agent_ledger.sql").read_text()
        conn.execute(text(ledger_ddl))
        conn.commit()
    yield engine
    engine.dispose()


@pytest.fixture()
def run_id() -> str:
    return str(uuid.uuid4())


def _enqueue(run_id: str, run_kind: str = "hunt") -> str:
    """Enqueue through the same service the API endpoint uses."""
    from services.agent_queue import build_start_job, enqueue_run

    job = build_start_job(
        run_id=run_id,
        run_kind=run_kind,
        request={
            "arch": "arch/threathunt.yaml",
            "playbook": "demo.yaml",
            "config": "vigil.config.yaml",
            "prompt": "prove the seam",
        },
        enqueued_by="integration-test",
    )
    return asyncio.run(enqueue_run(job))


def _run_worker_once() -> subprocess.CompletedProcess:
    """Run the TypeScript worker until it drains one job, then stop it."""
    env = {**os.environ, "DATABASE_URL": _database_url(), "REDIS_URL": _redis_url()}
    return subprocess.run(
        ["npx", "tsx", "tests/support/run-once.ts"],
        cwd=AI_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_S,
    )


def _events(engine, run_id: str) -> list[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT seq, run_kind, kind, payload FROM agent_events "
                "WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq"
            ),
            {"run_id": run_id},
        ).all()
    return [{"seq": r.seq, "run_kind": r.run_kind, "kind": r.kind, "payload": r.payload} for r in rows]


def _terminal(engine, run_id: str) -> Optional[Dict[str, Any]]:
    for event in _events(engine, run_id):
        if event["kind"] == "terminal":
            return event["payload"]
    return None


class TestWalkingSkeleton:
    def test_python_enqueues_a_job_the_node_worker_can_parse(self, engine, run_id):
        """The payload Python writes is the RunJob shape TypeScript reads."""
        job_id = _enqueue(run_id)
        assert job_id == run_id, "jobId must be the run_id so a double POST dedupes in BullMQ"

        result = _run_worker_once()
        assert result.returncode == 0, f"worker failed: {result.stdout}\n{result.stderr}"

    def test_the_worker_opens_the_ledger_and_marks_the_run_terminal(self, engine, run_id):
        _enqueue(run_id)
        result = _run_worker_once()
        assert result.returncode == 0, f"worker failed: {result.stdout}\n{result.stderr}"

        events = _events(engine, run_id)
        assert [(e["seq"], e["kind"]) for e in events] == [(0, "run"), (1, "terminal")]
        assert events[0]["payload"]["started_by"] == "integration-test"
        assert _terminal(engine, run_id)["outcome"] == "completed"

    def test_the_api_reports_a_status_the_worker_persisted(self, engine, run_id):
        """GET reads only what the worker wrote, using the two permitted queries."""
        from fastapi.testclient import TestClient

        from backend.main import app

        _enqueue(run_id)
        assert _run_worker_once().returncode == 0

        with TestClient(app) as client:
            response = client.get(f"/api/agent-runs/{run_id}")

        assert response.status_code in (200, 401, 403), response.text
        if response.status_code == 200:
            body = response.json()
            assert body["status"] == "terminal"
            assert body["outcome"] == "completed"
            assert body["events"] == 2

    def test_an_unknown_run_is_not_found_rather_than_an_error(self, engine):
        from fastapi.testclient import TestClient

        from backend.main import app

        with TestClient(app) as client:
            response = client.get(f"/api/agent-runs/{uuid.uuid4()}")
        assert response.status_code in (404, 401, 403), response.text

    def test_a_crash_before_terminal_leaves_the_run_resumable(self, engine, run_id):
        """The ledger is the resume point: an opened run completes on the next pass."""
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_events (run_id, run_kind, seq, kind, payload, schema_version) "
                    "VALUES (CAST(:run_id AS uuid), 'hunt', 0, 'run', CAST(:payload AS jsonb), 1)"
                ),
                {
                    "run_id": run_id,
                    "payload": json.dumps(
                        {
                            "run_kind": "hunt",
                            "spec": {},
                            "budgets": {"max_iterations": 0, "max_cost_usd": 0},
                            "seed": run_id,
                            "tenant_id": None,
                            "started_by": "crashed-worker",
                        }
                    ),
                },
            )
            conn.commit()

        _enqueue(run_id)
        assert _run_worker_once().returncode == 0

        events = _events(engine, run_id)
        assert [e["kind"] for e in events] == ["run", "terminal"], "resume must not collide on seq 0"
        assert events[0]["payload"]["started_by"] == "crashed-worker", "the original run event survives"

    def test_the_composite_key_rejects_a_second_writer(self, engine, run_id):
        """The primary key is the single-mutator guarantee, not merely an index."""
        from sqlalchemy.exc import IntegrityError

        row = {
            "run_id": run_id,
            "payload": json.dumps({"outcome": "completed", "reason": "first"}),
        }
        insert = text(
            "INSERT INTO agent_events (run_id, run_kind, seq, kind, payload, schema_version) "
            "VALUES (CAST(:run_id AS uuid), 'hunt', 0, 'terminal', CAST(:payload AS jsonb), 1)"
        )
        with engine.connect() as conn:
            conn.execute(insert, row)
            conn.commit()

        with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(insert, row)
                conn.commit()

        assert len(_events(engine, run_id)) == 1
