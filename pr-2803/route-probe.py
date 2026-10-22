import os, sys
os.environ.update(DEV_MODE="true", VIGIL_DISABLE_DOTENV="1")
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from services.api.main import app
from services.api.middleware.auth import get_current_user
from types import SimpleNamespace
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u1")
from services.api.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
c = TestClient(app)
c.cookies.set(CSRF_COOKIE_NAME, "t")
r = c.post("/api/conversations/import", json={"conversations": [{"id": "x", "messages": []}]}, headers={CSRF_HEADER_NAME: "t"})
print("POST /api/conversations/import ->", r.status_code, r.text[:120])
