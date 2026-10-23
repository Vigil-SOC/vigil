import asyncio, importlib.util, inspect, json, sys
import core.agents.tool_registry as new
spec = importlib.util.spec_from_file_location("old_tr", "/tmp/ev2966/old_tool_registry.py")
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
import core.findings.alert_outcomes as ao
ao.stored_finding_states = lambda data, ids: {i: ("open", None) for i in ids}
calls = []
class Fake:
    def get_findings_summary(self, **kw):
        calls.append(("summary", sorted(kw.items())))
        return {"total": 57, "by_severity": {"high": 3}, "by_state": {"open": 2}, "top_ips": ["1.2.3.4"]}
    def get_findings(self, **kw):
        calls.append(("get", sorted(kw.items())))
        return [{"finding_id": "f1", "severity": "high", "anomaly_score": 0.9, "description": "x" * 300, "entity_context": None}]
for m in (old, new): m._data = lambda: Fake()
cases = [{}, {"query": "evil", "severity": "high", "limit": 5, "offset": 50}, {"state": "bogus"}, {"state": "open", "sort_by": "timestamp"}]
bad = 0
for a in cases:
    for name in ("search_findings", "get_findings_stats"):
        calls.clear(); o = json.dumps(getattr(old, name)(**a)); oc = list(calls)
        calls.clear(); n = json.dumps(getattr(new, name)(**a)); nc = list(calls)
        same = o == n and oc == nc
        bad += not same
        print(name, a, "SAME" if same else f"DIFF\n old={o}\n new={n}\n oc={oc}\n nc={nc}")
from core.detections.tools import SecurityDetectionsTools
missing = [t for t in new._SECURITY_TOOLS if not inspect.iscoroutinefunction(getattr(SecurityDetectionsTools, t, None))]
print("security tools without coroutine method:", missing)
print("BACKEND_TOOLS is ALL_TOOLS:", new.BACKEND_TOOLS is __import__("core.llm.tool_schemas", fromlist=["x"]).ALL_TOOLS, len(new.BACKEND_TOOLS))
sys.exit(1 if bad or missing else 0)
