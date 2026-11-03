"""Curated demo scenarios: routine alerts settle alone, advanced ones need a person."""

from datetime import datetime, timezone

from core.findings.demo_reads import best_case_by_finding, terminal_state
from core.platform.demo_scenarios import AUTO, HUMAN, build_scenarios

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _built():
    findings, cases = build_scenarios(NOW)
    return findings, cases, best_case_by_finding(cases)


def test_deterministic_and_unique_ids():
    first, cases, _ = _built()
    second, _, _ = _built()
    assert first == second
    ids = [f["finding_id"] for f in first]
    assert len(ids) == len(set(ids))
    assert len({f["external_id"] for f in first}) == len(first)
    assert all(len(i) <= 50 for i in ids)
    assert len({c["case_id"] for c in cases}) == len(cases)


def test_sources_cover_wazuh_and_librenms_with_netbox_enrichment():
    findings, _, _ = _built()
    assert {f["data_source"] for f in findings} == {"wazuh", "librenms"}
    titles = " ".join(f["title"] for f in findings)
    assert "firewall-drop" in titles  # Wazuh IP blocks
    assert any(f["entity_context"].get("usernames") for f in findings)  # auth alerts
    assert any(
        f["data_source"] == "librenms"
        and "snmp" in f["entity_context"]["librenms"]["protocol"]
        for f in findings
    )
    netbox = [
        f["entity_context"]["netbox"]
        for f in findings
        if "netbox" in f["entity_context"]
    ]
    assert any(n["verified"] for n in netbox)
    # a known IP whose MAC is not the one on record: never verified
    mismatch = [n for n in netbox if n["ip_in_netbox"] and not n["verified"]]
    assert mismatch and all(
        n["recorded_mac"] and n["recorded_mac"] != n["observed_mac"] for n in mismatch
    )
    # an address NetBox has never seen
    assert any(not n["ip_in_netbox"] and not n["mac_in_netbox"] for n in netbox)


def test_a_mix_of_attack_techniques():
    findings, _, _ = _built()
    techniques = {t for f in findings for t in f["mitre_predictions"]}
    assert len(techniques) >= 8
    assert {"T1110.001", "T1078", "T1046", "T1602.001", "T1557.002"} <= techniques


def test_routine_alerts_are_settled_by_the_agent():
    findings, _, linked = _built()
    routine = [f for f in findings if f["scenario"] == AUTO]
    assert routine
    for f in routine:
        assert f["noise_marked"] and f["noise_marked_by_kind"] == "agent"
        assert f["ai_enrichment"]["recommended_action"] in {"dismiss", "monitor"}
        assert f["finding_id"] not in linked  # no case to open
        assert terminal_state(None, dismissed=True) == "dismissed"


def test_advanced_findings_always_need_a_person():
    findings, cases, linked = _built()
    advanced = [f for f in findings if f["scenario"] == HUMAN]
    assert advanced
    for f in advanced:
        assert not f.get("noise_marked")
        assert f["ai_enrichment"]["recommended_action"] in {"escalate", "investigate"}
        assert f["severity"] in {"high", "critical"}
        case = linked[f["finding_id"]]
        assert case["status"] == "open"
        assert terminal_state(case) == "needs_you"
    assert {f["finding_id"] for f in advanced} == set(linked)
    assert all(c["status"] == "open" for c in cases)


def test_every_case_finding_exists():
    findings, cases, _ = _built()
    ids = {f["finding_id"] for f in findings}
    assert all(fid in ids for c in cases for fid in c["finding_ids"])


def test_demo_service_serves_the_scenarios(monkeypatch):
    from core.platform.demo_data_service import DemoDataService

    monkeypatch.setattr(DemoDataService, "_initialized", False)
    service = DemoDataService()
    ids = {f["finding_id"] for f in service.get_findings()}
    assert "f-demo-wz-brute-then-success" in ids
    assert any(
        c["case_id"] == "case-demo-bastion-compromise" for c in service.get_cases()
    )
