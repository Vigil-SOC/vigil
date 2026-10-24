"""A completed sandbox report reaches every case its finding is linked to."""

import pytest
from sqlalchemy import delete, insert, select

from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Case, CaseEvidence, CaseIOC, Finding, case_findings
from core.storage.unit_of_work import unit_of_work
from services.daemon import sandbox_poller
from services.daemon.sandbox_poller import SandboxPoller

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

FID = "sbxlink-f-1"
LINKED = ("sbxlink-case-1", "sbxlink-case-2")
UNLINKED = "sbxlink-case-3"


@pytest.fixture(autouse=True)
def _seed():
    with unit_of_work() as s:
        s.execute(
            insert(Finding),
            [
                {
                    "finding_id": FID,
                    "data_source": "zeek",
                    "severity": "high",
                    "timestamp": sandbox_poller.utcnow(),
                    "ai_enrichment": {
                        "enrichment": {
                            "sandbox_submissions": {
                                "h1": {
                                    "anyrun": {
                                        "task_id": "t-1",
                                        "status": "submitted",
                                        "submitted_at": sandbox_poller.utcnow().isoformat(),
                                    }
                                }
                            }
                        }
                    },
                }
            ],
        )
        for cid in (*LINKED, UNLINKED):
            s.add(Case(case_id=cid, title=cid, status="open", priority="high"))
        s.flush()
        for cid in LINKED:
            s.execute(insert(case_findings).values(case_id=cid, finding_id=FID))
    yield
    with unit_of_work() as s:
        s.execute(delete(Case).where(Case.case_id.in_((*LINKED, UNLINKED))))
        s.execute(delete(Finding).where(Finding.finding_id == FID))


@pytest.mark.asyncio
async def test_report_reaches_every_linked_case(monkeypatch):
    ds = DatabaseDataService()
    all_findings = ds.get_findings
    monkeypatch.setattr(
        ds,
        "get_findings",
        lambda: [f for f in all_findings() if f["finding_id"] == FID],
    )
    poller = SandboxPoller(data_service=ds)

    async def fake_fetch(name, task_id):
        return {
            "data": {
                "verdict": "malicious",
                "scores": {"malconf": 9},
                "iocs": {"ip": ["1.2.3.4"]},
            }
        }

    monkeypatch.setattr(poller, "_fetch_report", fake_fetch)
    stats = await poller.run_once()
    assert stats["completed"] == 1

    with unit_of_work() as s:
        evidence = s.execute(select(CaseEvidence.case_id, CaseEvidence.name)).all()
        iocs = s.execute(select(CaseIOC.case_id, CaseIOC.value)).all()
    assert sorted(evidence) == [(cid, "anyrun report t-1") for cid in LINKED]
    assert sorted(iocs) == [(cid, "1.2.3.4") for cid in LINKED]
