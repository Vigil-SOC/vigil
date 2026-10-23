from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.workflows.workflows_router import router
from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate

from core.workflows.workflows_service import WorkflowsService
class NoCustom:
    def get(self, wid): return None
    def list(self, active_only=True): return []
app = FastAPI()
app.include_router(router, prefix="/api")
app.state.workflows = WorkflowsService(custom_workflows=NoCustom())
app.state.custom_workflows = NoCustom()
app.state.workflow_runs = SimpleNamespace(get_run=lambda r: None)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="u", user_id="u", principal_id="u", permissions=["*"], role=SimpleNamespace(permissions={"*": True}))
for r in router.routes:
    for d in getattr(r, "dependencies", []):
        app.dependency_overrides[d.dependency] = lambda: None
c = TestClient(app, raise_server_exceptions=False)
for m, p in [("GET","/api/workflows/custom"),("GET","/api/workflows/custom/x"),("POST","/api/workflows/reload"),("POST","/api/workflows/runs/r1/resume")]:
    r = c.request(m, p, json={"reason": "x"})
    print(m, p, r.status_code, r.text[:80])
    assert r.status_code < 500, (m, p, r.status_code)
