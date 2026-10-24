import hashlib, hmac, importlib.util, json, logging, sys
from pathlib import Path
from unittest.mock import patch, MagicMock
repo = Path(sys.argv[1]); sys.path.insert(0, str(repo))
logging.disable(logging.CRITICAL)
from fastapi import FastAPI
from fastapi.testclient import TestClient
def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, repo / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m
dt = load("dt_mod", "core/integrations/darktrace/darktrace_webhook_router.py")
cf = load("cf_mod", "core/integrations/cloudflare/cloudflare_webhooks_router.py")
from core import webhook_rejections as wr
app = FastAPI(); app.include_router(dt.router, prefix="/dt"); app.include_router(cf.router, prefix="/cf")
c = TestClient(app)
SECRET = "s3cret"
state = {"secret": SECRET, "enabled": True, "maxkb": 1, "finding": "auto", "persist": True}
def sec():
    if state["secret"] == "boom": raise RuntimeError("vault down")
    return state["secret"]
for m in (dt, cf): m._get_secret = sec
dt._get_settings = lambda: {"max_body_kb": state["maxkb"], "url": "https://dt"}
cf._get_max_body_bytes = lambda: state["maxkb"] * 1024
cf.cloudy_ingestion_enabled = lambda: state["enabled"]
class Svc:
    def __init__(self, *a, **k):
        self.ingestion_service = MagicMock(); self.ingestion_service.ingest_finding.side_effect = lambda f: state["persist"]
    def _t(self, p):
        if state["finding"] is None: return None
        return {"finding_id": "F", "data_source": "x"}
    transform_model_breach = transform_ai_analyst = transform_system_status = transform_event = _t
dt.DarktraceIngestionService = Svc
import core.integrations.cloudflare.ingestion as cing; cing.CloudyIngestionService = Svc
paths = {"dt/mb": ("/dt/model-breach", "X-Darktrace-Signature"), "dt/ss": ("/dt/system-status", "X-Darktrace-Signature"), "cf": ("/cf/cloudy", "X-Cloudflare-Signature")}
def go(case, body=b'{"a":1}', sig="ok", **st):
    old = dict(state); state.update(st)
    for k, (p, h) in paths.items():
        s = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest() if sig == "ok" else ("sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest() if sig == "pfx" else sig)
        hd = {h: s} if s else {}
        r = c.post(p, content=body, headers=hd)
        print(f"{case:14} {k:6} {r.status_code} {r.text}")
    state.clear(); state.update(old)
go("ok"); go("prefix", sig="pfx"); go("lookupfail", secret="boom"); go("nosecret", secret=None)
go("toobig", body=b'{"a":"' + b"x" * 2000 + b'"}'); go("missingsig", sig=""); go("badsig", sig="bad")
go("badjson", body=b"{nope"); go("array", body=b"[]"); go("nonutf8", body=b"\xff\xfe")
go("transformNone", finding=None); go("notpersisted", persist=False); go("disabled", enabled=False)
print(json.dumps(wr.rejection_counts(), sort_keys=True, indent=1))
