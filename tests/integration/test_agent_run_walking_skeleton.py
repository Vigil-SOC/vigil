# The cross-language seam, end to end: FastAPI enqueues onto BullMQ, a TypeScript
# worker consumes it, appends to agent_events, and the API reports the outcome.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.external_service]

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "services" / "agent"
WORKER_TIMEOUT_S = 60


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://vigil:vigil@localhost:5432/vigil_test")


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


# All three, not just the ledger: advance() takes a lease before it does anything,
# so a database with only 19 applied fails on the first job with `relation
# "agent_run_leases" does not exist`. Python does not model the agent tables (ADR
# 0001), so nothing else creates them for a test.
AGENT_DDL = (
    "19_agent_ledger.sql",
    "31_agent_ledger_hash_chain.sql",
    "20_agent_directives.sql",
    "21_agent_run_leases.sql",
)


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        for name in AGENT_DDL:
            ddl = (REPO_ROOT / "infra" / "database" / "init" / name).read_text()
            conn.execute(text(ddl))
        conn.commit()
    yield engine
    engine.dispose()


# What the lead emits, once, so the loop halts on its first decision. CONCLUDE is
# the hunt arch's only halting action, and it dispatches nobody, so one answer is
# the whole run.
DECISION = {
    "action": "CONCLUDE",
    "rationale": "the seam is proven",
    "evidence_citations": [],
}


# A model the run can reach, rather than one it pays. Neither CI nor a dev box runs
# Bifrost, so without this the worker opens the ledger, claims its lease and dies on
# the first completion -- which proves the seam up to the point it stops proving
# anything. Answering here keeps the test deterministic, free, and about the seam:
# Python enqueues, TypeScript consumes, the ledger is written, the API reports it.
class _StubModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": 0,
                "model": "stub/model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(DECISION),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def model_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def run_id() -> str:
    return str(uuid.uuid4())


# The seq-0 payload a real run writes, so the resume test can seed one. A resume
# reads the spec back off this event and re-reads no file, so a hand-written
# `spec: {}` never reaches the seq-0 collision it means to prove: the loop refuses
# an arch that declares no lead first, several steps earlier.
@pytest.fixture(scope="module")
def opened(engine, model_url) -> Dict[str, Any]:
    run_id = str(uuid.uuid4())
    _enqueue(run_id)
    assert _run_worker_once(model_url).returncode == 0
    return _events(engine, run_id)[0]["payload"]


def _enqueue(run_id: str, run_kind: str = "hunt") -> str:
    from core.agents.queue import build_start_job, enqueue_run

    job = build_start_job(
        run_id=run_id,
        run_kind=run_kind,
        request={
            # Empty arch routes through the run-kind registry. The playbook and
            # config are deployment-owned, so a test names fixtures, not defaults.
            "arch": "",
            "playbook": "tests/fixtures/hunt.playbook.yaml",
            "config": "tests/fixtures/hunt.config.yaml",
            "prompt": "prove the seam",
        },
        enqueued_by="integration-test",
    )
    return asyncio.run(enqueue_run(job))


def _run_worker_once(model_url: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": _database_url(),
        "REDIS_URL": _redis_url(),
        "BIFROST_URL": model_url,
    }
    return subprocess.run(
        ["npx", "tsx", "tests/support/run-once.ts"],
        cwd=AGENT_DIR,
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
    def test_python_enqueues_a_job_the_node_worker_can_parse(
        self, engine, run_id, model_url
    ):
        job_id = _enqueue(run_id)
        assert job_id == run_id, "jobId must be the run_id so a double POST dedupes in BullMQ"

        result = _run_worker_once(model_url)
        assert result.returncode == 0, f"worker failed: {result.stdout}\n{result.stderr}"

    def test_the_worker_opens_the_ledger_and_marks_the_run_terminal(
        self, engine, run_id, model_url
    ):
        _enqueue(run_id)
        result = _run_worker_once(model_url)
        assert result.returncode == 0, f"worker failed: {result.stdout}\n{result.stderr}"

        # The seam, not the loop's shape: the run opens at 0, the ledger has one
        # writer so its positions are contiguous, and it ends terminal. How many
        # events a decision takes on the way is the workflow's business and it
        # changes; pinning it here would make this test fail for the wrong reason.
        events = _events(engine, run_id)
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert events[0]["kind"] == "run"
        assert events[-1]["kind"] == "terminal"
        assert events[0]["payload"]["started_by"] == "integration-test"
        assert _terminal(engine, run_id)["outcome"] == "completed"

    def test_the_api_reports_a_status_the_worker_persisted(
        self, engine, run_id, model_url
    ):
        from fastapi.testclient import TestClient

        from services.api.main import app

        _enqueue(run_id)
        assert _run_worker_once(model_url).returncode == 0
        written = len(_events(engine, run_id))

        with TestClient(app) as client:
            response = client.get(f"/api/agent-runs/{run_id}")

        assert response.status_code in (200, 401, 403), response.text
        if response.status_code == 200:
            body = response.json()
            assert body["status"] == "terminal"
            assert body["outcome"] == "completed"
            assert body["events"] == written

    def test_an_unknown_run_is_not_found_rather_than_an_error(self, engine):
        from fastapi.testclient import TestClient

        from services.api.main import app

        with TestClient(app) as client:
            response = client.get(f"/api/agent-runs/{uuid.uuid4()}")
        assert response.status_code in (404, 401, 403), response.text

    def test_a_crash_before_terminal_leaves_the_run_resumable(
        self, engine, run_id, model_url, opened
    ):
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_events (run_id, run_kind, seq, kind, payload, schema_version) "
                    "VALUES (CAST(:run_id AS uuid), 'hunt', 0, 'run', CAST(:payload AS jsonb), 1)"
                ),
                {
                    "run_id": run_id,
                    "payload": json.dumps(
                        {**opened, "seed": run_id, "started_by": "crashed-worker"}
                    ),
                },
            )
            conn.commit()

        _enqueue(run_id)
        assert _run_worker_once(model_url).returncode == 0

        events = _events(engine, run_id)
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(len(events))), "resume must not collide on seq 0"
        assert events[-1]["kind"] == "terminal"
        assert events[0]["payload"]["started_by"] == "crashed-worker", "the original run event survives"

    def test_the_composite_key_rejects_a_second_writer(self, engine, run_id):
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
