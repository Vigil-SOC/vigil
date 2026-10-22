import logging, os, sys, warnings
warnings.simplefilter("ignore"); logging.disable(logging.CRITICAL)
os.environ.update(DEV_MODE="true", VIGIL_DISABLE_DOTENV="1")
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from services.api.main import app
from core.llm.providers.registry import ModelRegistry
paths = app.openapi()["paths"]
claude = sorted(p for p in paths if p.startswith("/api/claude"))
print("openapi /api/claude paths:", claude)
assert claude, "probe sees no claude routes at all"
assert "/api/claude/models" not in paths, "route still mounted"
assert "/api/ai/models" in paths, "replacement route missing"
assert not hasattr(ModelRegistry, "fallback_models"), "fallback_models still defined"
r = TestClient(app).get("/api/claude/models")
print("GET /api/claude/models ->", r.status_code, r.text)
assert r.status_code == 404
print("OK")
