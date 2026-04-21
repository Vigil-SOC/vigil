"""Integration tests for the VStrike ingest endpoint.

Focus: the handler must read-modify-write `entity_context` so that a push
from VStrike does not clobber pre-existing keys (src_ip, hostname, etc).

The tests patch `backend.api.vstrike.data_service` with an in-memory fake
so no DB / Redis is required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
# backend/main.py adds both the repo root and `backend/` to sys.path at
# runtime so that imports written as `from api.findings import ...` resolve.
# Mirror that here for standalone pytest runs.
for _p in (ROOT, ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DEV_MODE", "true")


class _FakeDataService:
    """In-memory stand-in for DatabaseDataService."""

    def __init__(self, seed: Optional[List[Dict[str, Any]]] = None):
        self._findings: Dict[str, Dict[str, Any]] = {}
        self.created_cases: List[Dict[str, Any]] = []
        for f in seed or []:
            self._findings[f["finding_id"]] = dict(f)

    def get_finding(self, finding_id: str):
        return self._findings.get(finding_id)

    def update_finding(self, finding_id: str, **updates) -> bool:
        if finding_id not in self._findings:
            return False
        self._findings[finding_id].update(updates)
        return True

    def create_finding(self, finding_data: Dict[str, Any]):
        fid = finding_data["finding_id"]
        stored = dict(finding_data)
        stored.setdefault("severity", "medium")
        stored.setdefault("status", "new")
        self._findings[fid] = stored
        return stored

    def create_case(
        self,
        title: str,
        finding_ids: List[str],
        priority: str = "medium",
        description: str = "",
        status: str = "open",
    ):
        case = {
            "case_id": f"case-test-{len(self.created_cases)+1:04d}",
            "title": title,
            "finding_ids": list(finding_ids),
            "priority": priority,
            "description": description,
            "status": status,
        }
        self.created_cases.append(case)
        return case


def _push_payload(finding_id: str, **overrides) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "batch_id": "test-batch-1",
        "findings": [
            {
                "finding_id": finding_id,
                "vstrike_enrichment": {
                    "asset_id": "srv-01",
                    "asset_name": "SAP-PROD-01",
                    "segment": "mgmt-vlan-10",
                    "site": "JBSA",
                    "criticality": "high",
                    "mission_system": "C2-AWACS",
                    "attack_path": ["ext-gw-01", "dmz-web-02", "srv-01"],
                    "blast_radius": 14,
                    "adjacent_assets": [
                        {
                            "asset_id": "dc-01",
                            "hop_distance": 1,
                            "edge_technique": "T1021.002",
                        }
                    ],
                    "enriched_at": "2026-05-20T14:00:00Z",
                },
            }
        ],
        "auto_cluster_cases": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def fake_service():
    return _FakeDataService(
        seed=[
            {
                "finding_id": "f-existing-1",
                "anomaly_score": 0.5,
                "timestamp": "2026-05-20T13:55:00Z",
                "data_source": "splunk",
                "entity_context": {
                    "src_ip": "10.1.1.5",
                    "hostname": "workstation-42",
                    "user": "jdoe",
                },
            }
        ]
    )


def _invoke_ingest(fake_service: _FakeDataService, payload: Dict[str, Any]):
    """Call the handler with `data_service` patched to the fake.

    Patches both the imported reference in `backend.api.vstrike` and the
    one used by `services.case_automation_service.cluster_findings_by_attack_path`.
    """
    import asyncio

    from backend.api import vstrike as vstrike_module
    from backend.schemas.vstrike import VStrikePushRequest

    req = VStrikePushRequest(**payload)

    with patch.object(vstrike_module, "data_service", fake_service), patch(
        "services.database_data_service.DatabaseDataService",
        return_value=fake_service,
    ):
        response = asyncio.run(vstrike_module.ingest_findings(req))
    return response


def test_entity_context_merge_preserves_existing_keys(fake_service):
    """Critical path: pushing VStrike enrichment must not clobber existing keys."""
    response = _invoke_ingest(fake_service, _push_payload("f-existing-1"))

    assert response.received == 1
    assert response.updated == 1
    assert response.created == 0
    assert response.failed == 0

    stored = fake_service.get_finding("f-existing-1")
    ctx = stored["entity_context"]
    # Pre-existing keys are preserved
    assert ctx["src_ip"] == "10.1.1.5"
    assert ctx["hostname"] == "workstation-42"
    assert ctx["user"] == "jdoe"
    # VStrike enrichment is nested
    assert ctx["vstrike"]["asset_id"] == "srv-01"
    assert ctx["vstrike"]["segment"] == "mgmt-vlan-10"
    assert ctx["vstrike"]["adjacent_assets"][0]["edge_technique"] == "T1021.002"


def test_entity_context_extra_merges_into_top_level(fake_service):
    """entity_context_extra from the payload is merged into top-level ctx."""
    payload = _push_payload("f-existing-1")
    payload["findings"][0]["entity_context_extra"] = {
        "dst_ip": "10.1.1.99",
        "segment_hint": "dmz",
    }

    _invoke_ingest(fake_service, payload)

    ctx = fake_service.get_finding("f-existing-1")["entity_context"]
    assert ctx["src_ip"] == "10.1.1.5"  # preserved
    assert ctx["dst_ip"] == "10.1.1.99"  # added
    assert ctx["segment_hint"] == "dmz"  # added
    assert "vstrike" in ctx


def test_create_path_when_finding_is_new(fake_service):
    payload = _push_payload("f-new-1")
    payload["findings"][0]["timestamp"] = "2026-05-20T14:00:00Z"
    payload["findings"][0]["anomaly_score"] = 0.91

    response = _invoke_ingest(fake_service, payload)

    assert response.updated == 0
    assert response.created == 1
    stored = fake_service.get_finding("f-new-1")
    assert stored["data_source"] == "vstrike"
    assert stored["entity_context"]["vstrike"]["asset_id"] == "srv-01"


def test_create_path_fails_without_minimum_fields(fake_service):
    payload = _push_payload("f-new-broken")
    # No timestamp / anomaly_score supplied
    response = _invoke_ingest(fake_service, payload)

    assert response.created == 0
    assert response.failed == 1
    assert "timestamp" in (response.results[0].error or "")


def test_auto_cluster_creates_case(fake_service):
    response = _invoke_ingest(fake_service, _push_payload("f-existing-1"))

    assert len(response.case_ids) == 1
    case = fake_service.created_cases[0]
    assert case["title"] == "VStrike: mgmt-vlan-10 via ext-gw-01"
    assert "f-existing-1" in case["finding_ids"]


def test_mitre_fields_propagate_on_update(fake_service):
    payload = _push_payload("f-existing-1")
    payload["findings"][0]["mitre_predictions"] = {"T1021.002": 0.87}
    payload["findings"][0]["predicted_techniques"] = [
        {"technique_id": "T1021.002", "confidence": 0.87}
    ]
    payload["findings"][0]["severity"] = "high"

    _invoke_ingest(fake_service, payload)

    stored = fake_service.get_finding("f-existing-1")
    assert stored["mitre_predictions"] == {"T1021.002": 0.87}
    assert stored["severity"] == "high"
    assert stored["predicted_techniques"][0]["technique_id"] == "T1021.002"


def test_auth_bypass_in_dev_mode():
    """verify_inbound_key returns without error when DEV_MODE=true."""
    from backend.api.vstrike import verify_inbound_key

    os.environ["DEV_MODE"] = "true"
    # No Authorization header → should still pass (no exception)
    verify_inbound_key(authorization=None)


def test_auth_requires_token_when_dev_mode_off(monkeypatch):
    """verify_inbound_key rejects missing token when DEV_MODE is off and a key is configured."""
    from fastapi import HTTPException

    from backend.api import vstrike as vstrike_module

    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setattr(
        vstrike_module, "_expected_inbound_key", lambda: "secret-key"
    )
    with pytest.raises(HTTPException) as excinfo:
        vstrike_module.verify_inbound_key(authorization=None)
    assert excinfo.value.status_code == 401


def test_auth_rejects_wrong_token(monkeypatch):
    from fastapi import HTTPException

    from backend.api import vstrike as vstrike_module

    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setattr(
        vstrike_module, "_expected_inbound_key", lambda: "secret-key"
    )
    with pytest.raises(HTTPException) as excinfo:
        vstrike_module.verify_inbound_key(authorization="Bearer wrong")
    assert excinfo.value.status_code == 401


def test_auth_accepts_correct_token(monkeypatch):
    from backend.api import vstrike as vstrike_module

    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setattr(
        vstrike_module, "_expected_inbound_key", lambda: "secret-key"
    )
    # Should not raise
    vstrike_module.verify_inbound_key(authorization="Bearer secret-key")
