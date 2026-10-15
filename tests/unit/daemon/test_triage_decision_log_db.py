"""A daemon triage verdict reaches the AI Decisions list, against real Postgres.

DB-backed because the row carries a foreign key to the finding it judged, and
because the confidence column is nullable only once the schema says so.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import AIDecisionLog, Finding
from core.storage.schemas import AIDecisionLogSchema
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work
from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = (
    REPO / "infra" / "database" / "init" / "42_ai_decision_confidence_nullable.sql"
)


def _triage(finding_id: str, reply: str) -> FindingProcessor:
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=True, auto_enrich_enabled=False)
    )
    processor._data_service = DatabaseDataService()
    processor._llm_gateway = Mock()
    processor._llm_gateway.submit = AsyncMock(return_value={"content": reply})
    with patch.object(
        FindingProcessor, "_resolve_triage_target", return_value=("prov-1", "m")
    ), patch.object(FindingProcessor, "_evaluate_for_response", AsyncMock()):
        asyncio.run(processor._enrich_in_background({"finding_id": finding_id}))
    return processor


def test_a_verdict_without_a_confidence_is_listed_under_the_triage_agent():
    with unit_of_work() as session:
        session.add(Finding(finding_id="f-triage-log", data_source="splunk"))

    _triage("f-triage-log", "SEVERITY: critical\nRECOMMENDED_ACTION: block")

    [row] = DatabaseService().list_ai_decisions(
        agent_id="triage", finding_id="f-triage-log", has_feedback=False
    )
    dumped = AIDecisionLogSchema.dump(row)
    assert dumped["decision_type"] == "triage"
    assert dumped["recommended_action"] == "block"
    assert dumped["confidence_score"] is None
    assert dumped["decision_metadata"]["severity"] == "critical"
    assert dumped["timestamp"]


def test_a_finding_with_no_row_writes_no_decision(caplog):
    # Ingest answers True for an external-id duplicate under a new finding id
    # (#1935) without storing it; there is no row for a decision to point at.
    with caplog.at_level(logging.ERROR, logger="services.daemon.processor"):
        processor = _triage("f-never-stored", "SEVERITY: high\nCONFIDENCE: 0.9")

    assert processor.stats["triaged"] == 1
    with unit_of_work() as session:
        assert (
            session.query(AIDecisionLog)
            .filter(AIDecisionLog.finding_id == "f-never-stored")
            .count()
            == 0
        )
    assert not any("triage decision" in r.getMessage() for r in caplog.records)


def _is_nullable(conn) -> str:
    return conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'ai_decision_logs' "
            "AND column_name = 'confidence_score'"
        )
    ).scalar_one()


def _migrate_step():
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", REPO / "scripts" / "migrate_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_ai_decision_confidence_nullable


@pytest.mark.parametrize("via", ["migrate_schema", "init_sql"])
def test_a_not_null_column_from_before_is_relaxed(via):
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            assert _is_nullable(conn) == "YES"  # what create_all builds
            # The schema as it was: no NULLs yet. Rolled back with the rest.
            conn.execute(
                text("DELETE FROM ai_decision_logs WHERE confidence_score IS NULL")
            )
            conn.execute(
                text(
                    "ALTER TABLE ai_decision_logs "
                    "ALTER COLUMN confidence_score SET NOT NULL"
                )
            )
            assert _is_nullable(conn) == "NO"
            if via == "migrate_schema":
                _migrate_step()(conn)
            else:
                conn.execute(text(INIT_SQL.read_text(encoding="utf-8")))
            assert _is_nullable(conn) == "YES"
        finally:
            tx.rollback()
