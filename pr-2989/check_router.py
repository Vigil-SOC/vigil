from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import services.api.routers.orchestrator as r
from services.api.middleware.auth import get_current_active_user
from core.auth.current_user import get_current_user

user = MagicMock(user_id="u1", username="alice")
app = FastAPI(); app.include_router(r.router, prefix="/api/orchestrator")
app.dependency_overrides[get_current_active_user] = lambda: user
app.dependency_overrides[get_current_user] = lambda: user
for dep in r._SETTINGS_WRITE + r._RUN_AGENTS + r._KILL:
    app.dependency_overrides[dep.dependency] = lambda: None
c = TestClient(app)
orch = MagicMock(); orch.get_all_investigations.return_value = []; orch.get_cost_summary.return_value = {}
orch.stats = {}; orch.config.max_concurrent_agents = 7; orch.get_investigation.return_value = None
orch.workdir.read_file.return_value = ""; orch.workdir.exists.return_value = False
db = MagicMock(); db.session_scope.return_value.__enter__.return_value.query.return_value.filter_by.return_value.count.return_value = 0
r._cached_orchestrator = orch
with patch("core.storage.connection.get_db_manager", return_value=db):
    svc = MagicMock(); svc.get_system_config.return_value = {"enabled": True, "max_concurrent_agents": 5}
    with patch.object(r, "get_config_service", return_value=svc):
        s = c.get("/api/orchestrator/status").json(); print("status db ok:", s["enabled"], s["max_concurrent_agents"])
        assert (s["enabled"], s["max_concurrent_agents"]) == (True, 5)
    with patch.object(r, "get_config_service", side_effect=RuntimeError("db down")):
        s = c.get("/api/orchestrator/status").json(); print("status db down:", s["enabled"], s["max_concurrent_agents"])
        assert (s["enabled"], s["max_concurrent_agents"]) == (False, 7)
with patch.object(r, "_persist_orchestrator_enabled") as p:
    print("enable:", c.post("/api/orchestrator/enable").json(), p.call_args)
    print("disable:", c.post("/api/orchestrator/disable").json(), p.call_args)
assert not orch.method_calls or all(m[0] not in ("enable", "disable") for m in orch.method_calls)
print("cost:", c.get("/api/orchestrator/cost").status_code)
for path, meth in [("/investigations/x", "get"), ("/investigations/x/files/a.json", "get"),
                   ("/investigations/x/wake", "post"), ("/investigations/x/chain-of-custody", "get"),
                   ("/investigations/x/export", "get")]:
    with patch("core.storage.connection.get_db_manager", return_value=db):
        db.session_scope.return_value.__enter__.return_value.query.return_value.filter_by.return_value.first.return_value = None
        resp = getattr(c, meth)("/api/orchestrator" + path); print(meth, path, resp.status_code, resp.json()["detail"])
        assert resp.status_code == 404
r._cached_orchestrator = None
with patch.object(r, "_get_orchestrator", return_value=None):
    for path, meth, code in [("/investigations/purge", "post", 503), ("/investigations/x/kill", "post", 404)]:
        resp = getattr(c, meth)("/api/orchestrator" + path); print(meth, path, resp.status_code); assert resp.status_code == code
print("ALL OK")
