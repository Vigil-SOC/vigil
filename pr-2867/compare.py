"""Old (HEAD) vs new: _ranking, rank_intake_row; old _source_text vs new _source_column SQL."""
import importlib.util, itertools, sys
from datetime import datetime, timedelta
sys.path.insert(0, ".")
from sqlalchemy import create_engine, select, literal, String
from sqlalchemy.sql import visitors

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

old_orch = load("old_orch", "/tmp/ev2860/old_orchestrator.py")
old_tr = load("old_tr", "/tmp/ev2860/old_triage_read.py")
from services.api import triage_read as new_tr
from services.daemon import orchestrator as new_orch
from services.daemon.config import OrchestratorConfig
from core.storage.models import IntakeTrigger, Finding

NOW = datetime(2099, 7, 1, 12)
cfg = OrchestratorConfig()
ttl, frac = cfg.intake_ttl_seconds, cfg.intake_ttl_promote_fraction
ages = [None, -10, 0, 1, ttl*(1-frac)-1, ttl*(1-frac), ttl*(1-frac)+1, ttl-1, ttl, ttl+5]
sevs = [None, "", " ", "critical", "HIGH", " medium ", "low", "bogus", 3]
kinds = ["detection", "schedule", "human_ask", None, "other"]
n = 0
for age, sev, pri, kind, has_f in itertools.product(ages, sevs, sevs, kinds, [True, False]):
    row = {"kind": kind, "priority": pri, "created_at": None if age is None else NOW - timedelta(seconds=age)}
    if has_f: row["_finding"] = {"severity": sev}
    assert new_tr._ranking(row, NOW, cfg) == old_tr._ranking(row, NOW, cfg), row
    kw = dict(now=NOW, ttl_seconds=ttl, promote_fraction=frac)
    assert new_orch.rank_intake_row(row, **kw) == old_orch.rank_intake_row(row, **kw), row
    n += 1
print("rank inputs compared:", n, "all equal")

eng = create_engine("sqlite://")
expr = new_tr._source_column()
m = 0
with eng.connect() as c:
    for kind, ds, has_f in itertools.product(["detection","schedule","human_ask","other",None], [None,"","splunk","0","  "], [True, False]):
        def rep(el):
            if getattr(el, "key", None) == "kind" and getattr(el, "table", None) is IntakeTrigger.__table__:
                return literal(kind, String)
            if getattr(el, "key", None) == "data_source" and getattr(el, "table", None) is Finding.__table__:
                return literal(ds if has_f else None, String)
        sql_val = c.execute(select(visitors.replacement_traverse(expr, {}, rep))).scalar()
        py_val = old_tr._source_text(kind, {"data_source": ds} if has_f else None)
        assert sql_val == py_val, (kind, ds, has_f, sql_val, py_val)
        m += 1
print("source cases compared:", m, "all equal")
print("_source_text still defined:", hasattr(new_tr, "_source_text"))
