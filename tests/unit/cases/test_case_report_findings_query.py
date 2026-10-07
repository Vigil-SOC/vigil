"""The case report fetches its findings in a constant number of queries.

``POST /api/cases/{id}/generate-report`` used to call ``get_finding`` once
per linked finding, so the statement count grew with the case. These tests
count the statements that read the ``findings`` table and check the same
findings, in the case's order, reach the report builder.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator, List

import pytest
from sqlalchemy import event

from core.storage.connection import get_db_manager
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# Any SELECT that returns finding rows, direct or through the case link: the
# column sits in the select list, before the first FROM. SQLAlchemy 2.1 no
# longer labels it ``AS findings_finding_id``, so match the column alone.
_FINDINGS_READ = re.compile(
    r"^\s*SELECT\b(?:(?!\bFROM\b).)*\bfindings\.finding_id\b",
    re.IGNORECASE | re.DOTALL,
)


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


class _StubReport:
    """Stands in for the PDF builder and records what it was given."""

    def __init__(self):
        self.calls = []

    def generate_case_report(self, output_path, case, findings):
        self.calls.append((case, findings))
        return True


def _seed(prefix: str, count: int) -> str:
    service = DatabaseService()
    ids = [f"{prefix}-{i:02d}" for i in range(count)]
    for finding_id in ids:
        assert service.create_finding(
            finding_id=finding_id,
            mitre_predictions={"T1566.001": 0.5},
            anomaly_score=0.4,
            timestamp=utcnow(),
            data_source=prefix,
            severity="high",
            status="new",
        )
    case_id = f"{prefix}-case"
    assert service.create_case(case_id=case_id, title=prefix, finding_ids=ids)
    return case_id


async def _report(cases, case_id: str, stub: _StubReport):
    """Run the handler; return its statements and what reached the stub."""
    case = cases.data_service.get_case(case_id)
    with _statements() as seen:
        result = await cases.generate_case_report(case_id)
    assert result["success"] is True
    reached_case, reached = stub.calls[-1]
    return seen, case, reached


@pytest.mark.asyncio
async def test_report_findings_fetch_is_constant(monkeypatch, tmp_path):
    from services.api.routers import cases

    if not cases.data_service.is_using_database():
        pytest.skip("needs the database-backed data service")
    stub = _StubReport()
    monkeypatch.setattr(cases, "report_service", stub)
    monkeypatch.chdir(tmp_path)

    tag = f"rpt{utcnow().strftime('%H%M%S%f')}"
    small = _seed(f"{tag}-s", 3)
    large = _seed(f"{tag}-l", 20)

    small_sql, small_case, small_findings = await _report(cases, small, stub)
    large_sql, large_case, large_findings = await _report(cases, large, stub)

    small_reads = [s for s in small_sql if _FINDINGS_READ.search(s)]
    large_reads = [s for s in large_sql if _FINDINGS_READ.search(s)]
    # get_case loads the linked findings once; the findings fetch adds one more.
    assert len(small_reads) == 2, small_reads
    assert len(large_reads) == 2, large_reads
    assert len(large_sql) == len(small_sql)

    for case, findings in ((small_case, small_findings), (large_case, large_findings)):
        expected = [cases.data_service.get_finding(fid) for fid in case["finding_ids"]]
        assert [f["finding_id"] for f in findings] == case["finding_ids"]
        assert findings == expected
    assert len(small_findings) == 3
    assert len(large_findings) == 20
