"""SandboxPoller marks a sub reported only once its report is on the case."""

import pytest

from services.daemon import sandbox_poller
from services.daemon.sandbox_poller import SandboxPoller

REPORT = {"data": {"info": {"score": 9}}}


class _DataService:
    def __init__(self, finding):
        self.finding = finding

    def get_findings(self):
        return [self.finding]

    def update_finding(self, finding_id, ai_enrichment):
        self.finding = {**self.finding, "ai_enrichment": ai_enrichment}
        return True


class _Correlation:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = []

    def attach_report(self, case_id, sandbox_name, task_id, report):
        self.calls.append((case_id, sandbox_name, task_id))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("db down")
        return {"evidence_id": 1, "iocs_added": 0, "verdict": None}


def _finding(case_id="case-1"):
    return {
        "finding_id": "f-1",
        "case_id": case_id,
        "ai_enrichment": {
            "enrichment": {
                "sandbox_submissions": {
                    "h1": {
                        "cape": {
                            "task_id": "t1",
                            "submitted_at": sandbox_poller.utcnow().isoformat(),
                        }
                    }
                }
            }
        },
    }


def _poller(ds, correlation, fetches):
    poller = SandboxPoller(data_service=ds)
    poller._correlation = correlation

    async def _fetch(name, task_id):
        fetches.append(task_id)
        return REPORT

    poller._fetch_report = _fetch
    return poller


def _enrichment(ds):
    return ds.finding["ai_enrichment"]["enrichment"]


@pytest.mark.asyncio
async def test_failed_correlation_counts_an_error_and_is_retried():
    ds = _DataService(_finding())
    correlation = _Correlation(fail_times=1)
    fetches = []
    poller = _poller(ds, correlation, fetches)

    stats = await poller.run_once()
    assert stats["errors"] == 1 and stats["completed"] == 0
    sub = _enrichment(ds)["sandbox_submissions"]["h1"]["cape"]
    assert "status" not in sub
    assert "sandbox_reports" not in _enrichment(ds)

    stats = await poller.run_once()
    assert stats["errors"] == 0 and stats["completed"] == 1
    assert fetches == ["t1", "t1"]
    assert correlation.calls == [("case-1", "cape", "t1")] * 2
    assert _enrichment(ds)["sandbox_submissions"]["h1"]["cape"]["status"] == "reported"
    assert list(_enrichment(ds)["sandbox_reports"]) == ["h1:cape"]

    await poller.run_once()
    assert fetches == ["t1", "t1"]


@pytest.mark.asyncio
async def test_finding_without_case_is_reported_without_correlation():
    ds = _DataService(_finding(case_id=None))
    correlation = _Correlation(fail_times=0)
    poller = _poller(ds, correlation, [])

    stats = await poller.run_once()
    assert stats["completed"] == 1 and correlation.calls == []
    assert _enrichment(ds)["sandbox_submissions"]["h1"]["cape"]["status"] == "reported"
