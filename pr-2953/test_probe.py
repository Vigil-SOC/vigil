import itertools, os
from contextlib import contextmanager
from unittest.mock import Mock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from core.response.approval_service import ActionStatus, ActionType, ApprovalService
from core.response.config import ResponseConfig
from core.storage.models import ApprovalAction as Row

@compiles(postgresql.JSONB, "sqlite")
def _jsonb(t, c, **k): return "JSON"
@compiles(postgresql.ARRAY, "sqlite")
def _arr(t, c, **k): return "JSON"

@pytest.fixture
def svc():
    eng = create_engine("sqlite://"); Row.__table__.create(eng)
    S = sessionmaker(bind=eng, expire_on_commit=False); mgr = Mock()
    @contextmanager
    def scope():
        s = S()
        try: yield s; s.commit()
        finally: s.close()
    mgr.session_scope = scope
    cfg = Mock(); cfg.read_system_config.return_value = {"enabled": False}
    with patch("core.response.approval_service.get_db_manager", return_value=mgr), \
         patch("core.response.approval_service.get_config_service", return_value=cfg):
        yield ApprovalService(ResponseConfig()), S

OPS = ["approve", "reject", "executed", "failed"]
@pytest.mark.parametrize("conf", [0.5, 0.99])
@pytest.mark.parametrize("seq", list(itertools.product(OPS, repeat=3)))
def test_no_approved_row_has_executed_at(svc, conf, seq):
    s, S = svc
    a = s.create_action(action_type=ActionType.BLOCK_IP, title="t", description="d",
                        target="1.2.3.4", confidence=conf, reason="r", evidence=[])
    if os.environ.get("CONTROL"):
        from datetime import datetime
        with S() as x:
            r = x.get(Row, a.action_id); r.status = "approved"; r.executed_at = datetime(2020,1,1); x.commit()
    for op in seq:
        {"approve": lambda: s.approve_action(a.action_id, "x"),
         "reject": lambda: s.reject_action(a.action_id, "no"),
         "executed": lambda: s.mark_executed(a.action_id, {"ok": 1}),
         "failed": lambda: s.mark_failed(a.action_id, "e")}[op]()
        for r in s.list_actions(status=ActionStatus.APPROVED):
            assert r.executed_at is None, (seq, r)
