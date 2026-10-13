"""A daemon triage verdict reaches the AI Decisions list, against real Postgres.

DB-backed because the row carries a foreign key to the finding it judged.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.storage.models import Finding
from core.storage.schemas import AIDecisionLogSchema
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work
from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def test_the_verdict_is_listed_under_the_triage_agent():
    with unit_of_work() as session:
        session.add(Finding(finding_id="f-triage-log", data_source="splunk"))

    processor = FindingProcessor(ProcessingConfig())
    processor._llm_gateway = Mock()
    processor._llm_gateway.submit = AsyncMock(
        return_value={
            "content": "SEVERITY: critical\nCONFIDENCE: 0.9\n"
            "RECOMMENDED_ACTION: block\nREASONING: known bad IP"
        }
    )
    with patch.object(
        FindingProcessor, "_resolve_triage_target", return_value=("prov-1", "m")
    ):
        asyncio.run(processor._triage_finding({"finding_id": "f-triage-log"}))

    [row] = DatabaseService().list_ai_decisions(
        agent_id="triage", finding_id="f-triage-log", has_feedback=False
    )
    dumped = AIDecisionLogSchema.dump(row)
    assert dumped["decision_type"] == "triage"
    assert dumped["recommended_action"] == "block"
    assert dumped["confidence_score"] == 0.9
    assert dumped["decision_metadata"]["severity"] == "critical"
    assert dumped["timestamp"]
