import os
os.environ.setdefault("DEV_MODE", "true")
os.environ["VIGIL_CSRF_ENABLED"] = "false"
from fastapi.testclient import TestClient
from core.storage.models import User
from services.api import main as m
from services.api.middleware import auth as a
from core.reporting import ai_decisions_router as r

class Stub:
    def get_ai_decision_stats(self, **k): return dict(total_decisions=0,total_with_feedback=0,feedback_rate=0,agreement_rate=0,avg_accuracy_grade=0,outcomes={},total_time_saved_minutes=0,total_time_saved_hours=0,period_days=7)
    def list_ai_decisions(self, **k): return []
    def create_ai_decision(self, **k): return None
r.DatabaseService = Stub
u = User(user_id="v", username="v", email="v@t.l", password_hash="", role_id="role-viewer", is_active=True, mfa_enabled=False)
m.app.dependency_overrides[a.get_current_active_user] = lambda: u
m.app.dependency_overrides[a.get_current_user] = lambda: u
c = TestClient(m.app)
routes = sorted(f"{meth.upper()} {p}" for p, ops in m.app.openapi()["paths"].items() if p.startswith("/api/ai/decisions") for meth in ops)
print("\n".join(routes))
checks = [("POST", "/api/ai/decisions", 405), ("GET", "/api/ai/decisions/dec-1", 404),
          ("GET", "/api/ai/decisions/stats", 200), ("GET", "/api/ai/decisions/pending-feedback", 200), ("GET", "/api/ai/decisions", 200)]
bad = 0
for meth, path, want in checks:
    got = c.request(meth, path, json={"decision_id":"x","agent_id":"a","decision_type":"t","confidence_score":0.5,"reasoning":"r","recommended_action":"c"} if meth == "POST" else None).status_code
    print(meth, path, got, "ok" if got == want else f"WANTED {want}")
    bad += got != want
raise SystemExit(bad)
