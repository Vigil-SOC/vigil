from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.api.discovery import load_router_specs
app = FastAPI()
for name, router, meta in load_router_specs():
    if name in ("findings", "v1_findings"):
        for prefix in (meta.prefix, *meta.legacy_prefixes):
            app.include_router(router, prefix=prefix)
        print(name, "mounted at", (meta.prefix, *meta.legacy_prefixes), "DELETE:", [r.path for r in router.routes if "DELETE" in r.methods])
c = TestClient(app, raise_server_exceptions=False)
control = c.delete("/api/findings/all")
print("control DELETE /api/findings/all ->", control.status_code)
assert control.status_code not in (404, 405), "routing harness broken"
for path in ("/api/findings/f-test-1", "/api/v1/findings/f-test-1"):
    r = c.delete(path)
    print("DELETE", path, "->", r.status_code, r.text[:60])
    assert r.status_code in (404, 405), "a delete-by-id route answered"
print("OK: no delete-by-id route on either findings router")
