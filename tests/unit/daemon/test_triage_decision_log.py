"""An answered daemon triage leaves one AI decision row an analyst can grade."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.llm import outage
from core.storage.models import AIDecisionLog
from services.daemon.config import ProcessingConfig
from services.daemon.probes import PROBE_DATA_SOURCE
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit

REPLY = (
    "SEVERITY: high\nCONFIDENCE: 0.82\nCATEGORY: Malware\n"
    "RECOMMENDED_ACTION: isolate\nREASONING: beaconing to a known C2"
)


@pytest.fixture(autouse=True)
def _clean_outages():
    outage._failures.clear()
    yield
    outage._failures.clear()


class _Db:
    """Stands in for get_db_manager(): keeps what each session added."""

    def __init__(self):
        self.added = []

    @contextmanager
    def session_scope(self):
        session = Mock()
        session.add.side_effect = self.added.append
        yield session


def _processor(reply: str = REPLY) -> FindingProcessor:
    processor = FindingProcessor(ProcessingConfig())
    gateway = Mock()
    gateway.submit = AsyncMock(return_value={"content": reply})
    processor._llm_gateway = gateway
    return processor


def _triage(processor, finding, db, target=("prov-1", "model-x")):
    with patch.object(
        FindingProcessor, "_resolve_triage_target", return_value=target
    ), patch("core.storage.connection.get_db_manager", return_value=db):
        return asyncio.run(processor._triage_finding(finding))


def test_an_answered_triage_logs_its_verdict_under_the_triage_filter():
    db = _Db()
    finding = _triage(_processor(), {"finding_id": "f-1", "data_source": "x"}, db)

    assert finding["severity"] == "high"
    [row] = db.added
    assert isinstance(row, AIDecisionLog)
    assert row.decision_id.startswith("triage-")
    # The AI Decisions agent filter lists the registry's decision ids.
    triage_agent = next(r for r in BUILTIN_AGENTS if r["id"] == "triage")
    assert row.agent_id == triage_agent["decision_id"]
    assert (row.decision_type, row.finding_id) == ("triage", "f-1")
    assert (row.case_id, row.workflow_id) == (None, None)
    assert row.confidence_score == 0.82
    assert row.recommended_action == "isolate"
    assert row.reasoning == "beaconing to a known C2"
    assert row.decision_metadata == {
        "source": "auto_triage",
        "severity": "high",
        "category": "malware",
        "provider_id": "prov-1",
        "model": "model-x",
    }


def test_a_reply_without_a_confidence_is_logged_at_zero():
    db = _Db()
    _triage(
        _processor("SEVERITY: low\nRECOMMENDED_ACTION: monitor"),
        {"finding_id": "f-1"},
        db,
    )
    [row] = db.added
    assert (row.confidence_score, row.reasoning) == (0.0, "")
    assert row.decision_metadata["category"] is None


def test_a_failed_write_is_logged_once_and_triage_still_succeeds(caplog):
    broken = Mock()
    broken.session_scope.side_effect = RuntimeError("db down")
    processor = _processor()
    with caplog.at_level(logging.INFO, logger="services.daemon.processor"):
        for n in range(3):
            finding = _triage(processor, {"finding_id": f"f-{n}"}, broken)
            assert finding["ai_triage"]["result"]["recommended_action"] == "isolate"
            assert "ai_triage_error" not in finding
        _triage(processor, {"finding_id": "f-3"}, _Db())

    assert processor.stats["triaged"] == 4
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "f-0" in errors[0].getMessage()
    assert any("recovered after 3" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "target,reply,error",
    [
        (None, REPLY, "no LLM provider configured"),
        (("prov-1", "model-x"), "", "empty LLM response"),
    ],
)
def test_no_row_when_triage_got_no_answer(target, reply, error):
    db = Mock()
    finding = _triage(_processor(reply), {"finding_id": "f-1"}, db, target=target)
    assert finding["ai_triage_error"] == error
    db.session_scope.assert_not_called()


@pytest.mark.parametrize(
    "finding,reply",
    [
        ({"finding_id": "f-1", "data_source": PROBE_DATA_SOURCE}, REPLY),
        ({"finding_id": "f-1"}, "I can't assess this alert."),
    ],
    ids=["probe", "no-verdict"],
)
def test_no_row_for_a_probe_or_a_reply_with_no_verdict(finding, reply):
    db = Mock()
    finding = _triage(_processor(reply), finding, db)
    assert "ai_triage" in finding
    db.session_scope.assert_not_called()
