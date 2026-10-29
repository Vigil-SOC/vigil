"""One host is asked to be isolated once, whatever names it.

The retired daemon responder keyed an isolation on the alert's IP, or on the
hostname when the alert had no IP, and installs still hold those rows. An
investigation's proposal keys on its run. Rows about one host, with and without
its IP, and a run that proposes isolating it, all land on the one approval.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.agents.builtins import AgentId
from core.response.approval_service import ActionType, ApprovalService
from core.response.config import ResponseConfig
from core.storage.connection import get_db_manager
from core.workflows import run_bridge_router as rbr
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

IP, HOST = "192.168.70.186", "FYODOR-L"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == 0, report
        with engine.begin() as conn:
            for case_id in ("c-1", "c-2"):
                conn.execute(
                    text(
                        "INSERT INTO cases (case_id, title, description, timeline) "
                        "VALUES (:id, 'Hunt', '', '[]'::jsonb)"
                    ),
                    {"id": case_id},
                )
            conn.execute(
                text(
                    "INSERT INTO findings "
                    "(finding_id, data_source, created_at, updated_at, timestamp) "
                    "VALUES ('f-1', 'splunk', now(), now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO case_findings (case_id, finding_id, added_at) "
                    "VALUES ('c-2', 'f-1', now())"
                )
            )
        yield engine


@pytest.fixture
def db(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    manager = get_db_manager()
    saved = manager._engine, manager._session_factory
    manager._engine, manager._session_factory = engine, factory
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE approval_actions RESTART IDENTITY CASCADE"))
    try:
        yield engine
    finally:
        manager._engine, manager._session_factory = saved


def _isolate(ip: str, hostname: str | None) -> dict:
    """A row as the retired responder filed it: keyed on the IP, or the hostname
    when there is none."""
    key = ip if ip != "unknown" else f"host:{hostname}"
    action, inserted = ApprovalService(config=ResponseConfig())._put_action(
        action_type=ActionType.ISOLATE_HOST,
        title=f"Isolate Host: {hostname or ip}",
        description="test",
        target=ip,
        confidence=0.95,
        reason="encoded PowerShell",
        evidence=["f-1"],
        created_by=AgentId.AUTO_RESPONDER.value,
        parameters={"hostname": hostname},
        idempotency_key=f"isolate_host:{key}",
        same_host=(ip, hostname),
    )
    return {"action_id": action.action_id, "reused": not inserted}


def _propose(case_id: str, run_id: str, target: str) -> None:
    rbr._propose_actions(
        case_id,
        run_id,
        [
            {
                "action_type": "isolate_host",
                "target": target,
                "reason": "fodhelper UAC bypass",
                "confidence": 0.9,
            }
        ],
        ApprovalService(config=ResponseConfig()),
    )


def _isolations(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT action_id, idempotency_key, parameters FROM approval_actions "
                "WHERE action_type = 'isolate_host' ORDER BY created_at"
            )
        ).all()


@pytest.mark.parametrize(
    "first, second", [((IP, HOST), ("unknown", HOST)), (("unknown", HOST), (IP, HOST))]
)
def test_an_alert_with_the_ip_and_one_without_ask_once(db, first, second):
    filed = _isolate(*first)
    again = _isolate(*second)

    assert filed["reused"] is False
    assert again["reused"] is True
    assert again["action_id"] == filed["action_id"]
    assert len(_isolations(db)) == 1


def test_alerts_at_the_same_moment_ask_once(db):
    calls = [(IP, HOST), ("unknown", HOST)] * 4
    start = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def call(i: int, ip: str, hostname: str) -> None:
        start.wait()
        results[i] = _isolate(ip, hostname)

    threads = [threading.Thread(target=call, args=(i, *c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_isolations(db)) == 1
    assert len({r["action_id"] for r in results}) == 1


def test_a_hostname_matches_whatever_its_case(db):
    filed = _isolate(IP, HOST)

    assert _isolate("unknown", "fyodor-l")["action_id"] == filed["action_id"]


def test_a_run_proposal_joins_a_responder_row_on_its_case_and_the_alerts(db):
    filed = _isolate(IP, HOST)

    _propose("c-1", "run-1", HOST)

    rows = _isolations(db)
    assert [r.action_id for r in rows] == [filed["action_id"]]
    # The alert's case first, then the run's.
    assert rows[0].parameters["case_ids"] == ["c-2", "c-1"]


def test_a_run_on_the_alerts_own_case_joins_a_responder_row(db):
    filed = _isolate("unknown", HOST)

    _propose("c-2", "run-1", HOST)

    rows = _isolations(db)
    assert [r.action_id for r in rows] == [filed["action_id"]]
    assert rows[0].parameters["case_ids"] == ["c-2"]


def test_a_run_proposal_naming_the_ip_joins_a_responder_row(db):
    _isolate(IP, HOST)
    _isolate("unknown", HOST)

    _propose("c-1", "run-1", IP)

    assert len(_isolations(db)) == 1


def test_a_responder_row_joins_a_pending_run_proposal(db):
    _propose("c-1", "run-1", HOST)

    again = _isolate(IP, HOST)

    rows = _isolations(db)
    assert len(rows) == 1
    assert again["reused"] is True
    assert again["action_id"] == rows[0].action_id


def test_other_hosts_are_still_asked_about(db):
    _isolate(IP, HOST)

    _isolate("unknown", "BGIST-L")
    _isolate("192.168.70.187", None)
    _propose("c-1", "run-1", "BSTOLL-L")

    assert len(_isolations(db)) == 4


def test_a_failed_isolation_is_asked_again(db):
    filed = _isolate(IP, HOST)
    ApprovalService(config=ResponseConfig()).mark_failed(filed["action_id"], "EDR down")

    again = _isolate("unknown", HOST)

    assert again["reused"] is False
    assert again["action_id"] != filed["action_id"]
