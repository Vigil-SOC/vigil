"""Findings reads must not load the linked ``Case`` rows (#1439).

``Finding.cases`` is mapped ``lazy="selectin"``. Without an override every
findings read runs one more SELECT returning each linked case in full, JSONB
and all, and ``FindingSchema`` then drops it. The read paths in
``DatabaseService`` opt out; these tests count the statements that reach the
``cases`` table and check the dumps are unchanged.
"""

import re
from contextlib import contextmanager
from typing import Iterator, List

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import selectinload

from core.storage.connection import get_db_manager
from core.storage.models import Finding
from core.storage.schemas.finding import FindingSchema
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

_CASES_TABLE = re.compile(r"\bcases\b", re.IGNORECASE)
TECHNIQUE = "T1566.001"


@contextmanager
def _statements() -> Iterator[List[str]]:
    """Collect every SQL statement the engine executes inside the block."""
    engine = get_db_manager().engine
    seen: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def _cases_queries(statements: List[str]) -> List[str]:
    return [s for s in statements if _CASES_TABLE.search(s)]


def _seed(service: DatabaseService, prefix: str) -> List[str]:
    ids = [f"{prefix}-{i}" for i in range(4)]
    for i, finding_id in enumerate(ids):
        assert service.create_finding(
            finding_id=finding_id,
            mitre_predictions={TECHNIQUE: 0.5 + i / 10},
            anomaly_score=0.4,
            timestamp=utcnow(),
            data_source=prefix,
            severity="high",
            status="new",
        )
    big = [{"note": "x" * 2048} for _ in range(5)]
    assert service.create_case(
        case_id=f"{prefix}-case-a",
        title=f"{prefix} case A",
        finding_ids=ids[:3],
        timeline=big,
        notes=big,
        activities=big,
    )
    assert service.create_case(
        case_id=f"{prefix}-case-b", title=f"{prefix} case B", finding_ids=ids[1:]
    )
    return ids


def _baseline_dumps(ids: List[str]) -> dict:
    """Dumps from the mapping's default loading, cases selectin included."""
    with get_db_manager().session_scope() as session:
        rows = (
            session.execute(
                select(Finding)
                .options(selectinload(Finding.mitre_prediction_rows))
                .where(Finding.finding_id.in_(ids))
            )
            .scalars()
            .all()
        )
        assert any(row.cases for row in rows), "fixture links no cases"
        return {row.finding_id: FindingSchema.dump(row) for row in rows}


def test_get_findings_skips_cases_and_dumps_the_same():
    service = DatabaseService()
    ids = _seed(service, "noload-list")
    baseline = _baseline_dumps(ids)

    with _statements() as statements:
        findings = service.get_findings(data_source="noload-list", limit=100)
        dumped = {f.finding_id: FindingSchema.dump(f) for f in findings}

    assert statements, "counter saw no statements"
    assert _cases_queries(statements) == []
    assert dumped == baseline
    assert "cases" not in next(iter(dumped.values()))


def test_get_finding_and_technique_reads_skip_cases():
    service = DatabaseService()
    ids = _seed(service, "noload-one")
    baseline = _baseline_dumps(ids)

    with _statements() as statements:
        one = service.get_finding(ids[1])
        by_technique = service.get_findings_by_technique(TECHNIQUE)
        missing = service.get_findings_missing_enrichment(limit=100)

    assert _cases_queries(statements) == []
    assert one is not None
    assert FindingSchema.dump(one) == baseline[ids[1]]
    ours = {f.finding_id: FindingSchema.dump(f) for f in by_technique}
    assert {k: ours[k] for k in ids} == baseline
    assert {d["finding_id"]: d for d in missing if d["finding_id"] in baseline} == (
        baseline
    )


def test_update_finding_skips_cases_and_links_survive():
    service = DatabaseService()
    ids = _seed(service, "noload-update")

    with _statements() as statements:
        assert service.update_finding(ids[1], severity="low")

    assert _cases_queries(statements) == []
    with get_db_manager().session_scope() as session:
        finding = session.get(Finding, ids[1])
        assert finding is not None
        assert finding.severity == "low"
        assert len(finding.cases) == 2
