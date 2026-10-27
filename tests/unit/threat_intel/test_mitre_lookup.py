from core.threat_intel.mitre_lookup import (
    iter_techniques,
    rank_by_confidence,
    resolve_technique,
    top_technique,
)


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


def test_an_unknown_confidence_keeps_every_technique_on_the_map():
    techniques = list(
        iter_techniques(
            {"mitre_predictions": {"T1071": None, "T1059": 0.3, "T1105": True}}
        )
    )
    assert techniques == [
        {"technique_id": "T1071", "confidence": None},
        {"technique_id": "T1059", "confidence": 0.3},
        {"technique_id": "T1105", "confidence": None},
    ]


def test_a_single_technique_dict_is_not_read_as_a_map():
    for tech in (
        {"technique_id": "T1059", "confidence": 0.9, "technique_name": "x"},
        {"id": "T1059", "confidence": None},
    ):
        assert list(iter_techniques({"mitre_predictions": tech})) == [tech]


def test_predicted_techniques_is_not_read():
    finding = {"predicted_techniques": [{"technique_id": "T1048", "confidence": 1}]}
    assert list(iter_techniques(finding)) == []
    nested = {"mitre_predictions": {"predicted_techniques": [{"technique_id": "T1"}]}}
    assert list(iter_techniques(nested)) == []


def test_scored_first_by_confidence_then_unknowns_in_source_order():
    ranked = rank_by_confidence(
        iter_techniques(
            {
                "mitre_predictions": {
                    "U1": None,
                    "A": 0.5,
                    "U2": "high",
                    "B": 0.9,
                    "C": 0.5,
                    "Z": 0,
                }
            }
        )
    )
    assert [t["technique_id"] for t in ranked] == ["B", "A", "C", "Z", "U1", "U2"]


def test_top_is_the_first_source_named_when_none_is_scored():
    mitre = {"mitre_predictions": {"T1071": None, "T1059": None}}
    assert top_technique(iter_techniques(mitre))["technique_id"] == "T1071"
    assert top_technique([]) is None
