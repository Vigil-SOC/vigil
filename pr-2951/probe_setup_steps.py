"""Calls the real GET /config/setup-steps through FastAPI with response_model on.
Fails loud if response_model drops, renames, or rejects any field build_setup_steps returns."""
import os, tempfile
os.environ.update({"DEV_MODE": "true", "VIGIL_DISABLE_DOTENV": "1", "VIGIL_DIR": tempfile.mkdtemp()})
from unittest.mock import MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
import services.api.routers.config as cfg
from core.routing import request_unit_of_work
from core.deps import provide_detection_rules

for skills_root, customs, alerts, demo in [(None, 0, 0, False), ("/x", 3, 7, True)]:
    rules = MagicMock(); rules.list_sources.return_value = [{"status": "ready", "rule_count": 2}]
    cfg.load_integrations_config = lambda _svc: {"integrations": {"splunk": {}}}
    cfg.get_config_service = lambda: None
    cfg._secrets_set_map = lambda _i: {"slack": {"bot_token": True}}
    cfg.assigned_model_ids = lambda _s: {"a", "b"}
    cfg.iter_descriptors = lambda: [1, 2, 3]
    cfg.findings_data_service = MagicMock(count_findings=lambda: alerts)
    cfg.is_demo_mode = lambda: demo
    cfg.operator_skills_root = lambda: skills_root
    cfg.skill_roots = lambda: []
    cfg.load_skills = lambda _r: [object()] * customs
    cfg.is_bundled = lambda _s: False
    app = FastAPI(); app.include_router(cfg.router, prefix="/api/config")
    app.dependency_overrides[request_unit_of_work] = lambda: MagicMock()
    app.dependency_overrides[provide_detection_rules] = lambda: rules
    res = TestClient(app).get("/api/config/setup-steps")
    expected = cfg.build_setup_steps(
        loaded={"integrations": {"splunk": {}}}, secrets_set={"slack": {"bot_token": True}},
        sources=rules.list_sources(), model_ids={"a", "b"}, descriptor_count=3,
        alerts_exist=alerts, demo_enabled=demo, skills_root_set=skills_root is not None, custom_skills=customs)
    print(res.status_code, [s["id"] for s in res.json()["steps"]], res.json()["alerts_exist"], res.json()["demo_enabled"])
    assert res.status_code == 200, res.text
    assert res.json() == expected, (res.json(), expected)
print("route body == build_setup_steps output in both cases")

from pydantic import ValidationError
from core.api.v1.approvals_router import NeedsYouItem
base = dict(source_id="s", title="t", reason="r", created_at="")
for kind in ("approval", "checkpoint", "parked"):
    for rev in ("reversible", "irreversible"):
        NeedsYouItem(kind=kind, reversibility=rev, **base)
try:
    NeedsYouItem(kind="other", reversibility="reversible", **base); raise SystemExit("unknown kind accepted")
except ValidationError:
    print("unknown kind rejected")
