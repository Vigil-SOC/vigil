"""Throwaway probe for #2900. Runs the real create + enqueue + cross-correlation."""
import json, os, re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.daemon.orchestrator as orch_mod
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import CaseSpec, Orchestrator
from services.daemon.workdir import WorkdirManager

FINDING = {"finding_id": "f-1", "severity": "high", "description": "lockouts",
           "entity_context": {"src_ip": "10.0.0.1", "hostname": "h1"},
           "mitre_predictions": {"T1110": 0.9}}


def _orch(tmp_path):
    o = object.__new__(Orchestrator)
    o.config = OrchestratorConfig(dry_run=False)
    o.workdir = WorkdirManager(str(tmp_path))
    o._workflows = MagicMock()
    o.shared_intel = MagicMock()
    o.stats = {"investigations_created": 0}
    o._save_investigation = MagicMock(return_value=True)
    o._check_cross_correlations = AsyncMock()
    return o


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["finding", "hunt"])
async def test_probe(tmp_path, kind):
    o = _orch(tmp_path)
    captured = []

    async def fake_enqueue(job, job_id=None):
        captured.append(job)

    runs = MagicMock(); runs.get_run.return_value = None
    with patch.object(orch_mod, "enqueue_run", fake_enqueue), \
         patch.object(orch_mod, "WorkflowRunService", return_value=runs), \
         patch.object(Orchestrator, "_declared_run_kind", return_value="investigation"), \
         patch.object(Orchestrator, "_hypothesis_subjects", return_value=[]), \
         patch.object(Orchestrator, "_recall_keys", return_value=[]):
        if kind == "finding":
            await o._create_investigation(workflow_id="incident-response", findings=[FINDING],
                trigger_type="finding", priority="high",
                mint_case=CaseSpec(title="t", finding_ids=["f-1"], priority="high"))
        else:
            await o._create_investigation(workflow_id="threat-hunt", findings=[],
                trigger_type="scheduled", priority="medium", hypothesis="beaconing to x")
    rec = o._save_investigation.call_args[0][0]
    inv = rec["investigation_id"]
    blob = json.dumps(captured, default=str, sort_keys=True)
    norm = re.sub(r"inv-\d{8}-[0-9a-f]{8}", "INV", blob)
    norm = re.sub(r"case-[0-9a-zA-Z-]+", "CASE", norm)
    norm = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+Z?", "TS", norm)
    print(f"\n[{kind}] files={sorted(os.listdir(tmp_path / inv))}")
    print(f"[{kind}] state keys={sorted(o.workdir.read_state(inv))}")
    print(f"[{kind}] record step fields={ {k: rec.get(k) for k in ('current_step','total_steps')} }")
    print(f"[{kind}] JOB={norm}")
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_cross_correlation_still_logs(tmp_path):
    o = _orch(tmp_path)
    del o._check_cross_correlations
    for i in ("inv-a", "inv-b"):
        o.workdir.create(i)
    o.shared_intel.get_related_investigations.return_value = ["inv-b"]
    o.shared_intel.get_shared_iocs.return_value = ["ip:1.2.3.4"]
    o.get_investigation = MagicMock(return_value=None)
    await o._check_cross_correlations("inv-a")
    for i in ("inv-a", "inv-b"):
        events = [e["event"] for e in o.workdir.get_log(i, tail=10)]
        print(f"\n{i} log events={events}")
        assert "cross_correlation" in events
