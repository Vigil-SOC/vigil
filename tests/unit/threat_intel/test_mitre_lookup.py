from core.threat_intel.mitre_lookup import resolve_technique


def test_resolves_ids_the_web_client_used_to_resolve_itself():
    assert resolve_technique("T1566") == ("T1566", "Phishing", "Initial Access")
    assert resolve_technique("T1003.001") == (
        "T1003.001",
        "LSASS Memory",
        "Credential Access",
    )
    assert resolve_technique("T1021.001") == (
        "T1021.001",
        "Remote Desktop Protocol",
        "Lateral Movement",
    )


def test_name_on_the_finding_beats_the_table():
    tech = {"technique_id": "T1566", "technique_name": "Custom", "tactic": "X"}
    assert resolve_technique(tech) == ("T1566", "Custom", "X")


def test_unknown_id_falls_back_to_the_id():
    assert resolve_technique("T9999.001") == ("T9999.001", "T9999.001", "Unknown")
