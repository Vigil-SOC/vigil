"""Every reader of the stored force flag agrees with the gate, real ConfigService, broken or stubbed session."""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import core.storage.config_service as cs
from core.agents.attribution import autonomy_label
from core.response.approval_service import ApprovalService, stored_force_manual_approval
from core.response.config import ResponseConfig
from services.api.routers.config import get_force_manual_approval
from services.daemon.config import DaemonConfig
from services.daemon.intent import effective_daemon_config, intent_report

ROWS = ["NOROW", {"enabled": True}, {"enabled": False}, {}, None, [1], "FAIL"]


class _Q:
    def __init__(self, row): self.row = row
    def filter_by(self, **_): return self
    def first(self):
        if self.row == "NOROW": return None
        class R: pass
        r = R(); r.value = self.row; return r


def _session_for(row):
    @contextmanager
    def s():
        if row == "FAIL":
            raise RuntimeError("db down")
        class S:
            def query(self, _m): return _Q(row)
        yield S()
    return s


@pytest.mark.parametrize("row", ROWS, ids=repr)
def test_readers_agree(row, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "get_session", _session_for(row))
    svc = cs.ConfigService()
    monkeypatch.setattr("core.response.approval_service.get_config_service", lambda: svc)
    monkeypatch.setattr(ResponseConfig, "from_settings", classmethod(lambda c: ResponseConfig(auto_response_enabled=True, force_manual_approval=False)))
    gate = stored_force_manual_approval()
    action = ApprovalService.__new__(ApprovalService)
    overlay = effective_daemon_config(DaemonConfig()).response.force_manual_approval
    label = autonomy_label()
    p = tmp_path / "INTENT.md"; p.write_text("---\nrespond:\n  force_manual_approval: false\n---\n")
    rows = {r.key: r for r in intent_report(DaemonConfig(), path=p, include_same=True)}
    report = rows["respond.force_manual_approval"].effective
    try:
        route = get_force_manual_approval().enabled
    except HTTPException as e:
        route = e.status_code
    print(f"\nrow={row!r:22} gate={gate} overlay={overlay} report={report} label={label} route={route}")
    assert overlay is gate and report is gate
    assert label == ("Assist" if gate else "Act")
    assert route == (503 if row == "FAIL" else gate)
