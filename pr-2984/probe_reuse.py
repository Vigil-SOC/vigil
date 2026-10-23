import time
from unittest.mock import patch
from services.api.routers import orchestrator as router

class Svc:
    def __init__(s, saved): s.saved = saved
    def get_system_config(s, key): return s.saved if key == "orchestrator.settings" else None

saved = {"workdir_base": "/tmp/ev2897/wd-a", "max_cost_per_investigation": 2.5, "enabled": False}
with patch("core.storage.config_service.get_config_service", return_value=Svc(saved)):
    a = router._get_orchestrator()
    t = time.perf_counter()
    for _ in range(50):
        b = router._get_orchestrator()
    per = (time.perf_counter() - t) / 50
    print("reused across 50 calls:", b is a, f"per-call {per*1000:.2f} ms")
    saved["enabled"] = True
    c = router._get_orchestrator()
    print("enabled flip rebuilds:", c is not a, "enabled:", c.enabled)
    saved["workdir_base"] = "/tmp/ev2897/wd-b"
    d = router._get_orchestrator()
    print("workdir change rebuilds:", d is not c, d.workdir.base_dir)
    print("per_investigation_limit:", d.config.max_cost_per_investigation)
    assert b is a and c is not a and str(d.workdir.base_dir) == "/tmp/ev2897/wd-b"
