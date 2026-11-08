"""An id that looks like ATT&CK but is not an active technique in the pinned release
is reported as unknown, never as a gap (#4048); names come from the same file."""

import pytest

from core.detections import gaps
from core.detections.inventory import catalog
from core.detections.tools import SecurityDetectionsTools
from core.threat_intel.mitre_lookup import resolve_technique


def _check(*ids):
    return gaps.technique_check(ids, {}, {})


def test_real_id_is_bucketed():
    out = _check("T1059")
    assert [r["technique"] for r in out["rows"]] == ["T1059"]
    assert out["by_bucket"]["none"] == 1
    assert out["gaps"] == ["T1059"]
    assert out["unknown"] == []


def test_well_formed_fake_is_unknown_not_a_gap():
    out = _check("T9999")
    assert out["rows"] == []
    assert out["gaps"] == []
    assert out["checked"] == 0
    assert sum(out["by_bucket"].values()) == 0
    assert out["rejected"] == []
    assert [u["technique"] for u in out["unknown"]] == ["T9999"]
    assert out["unknown"][0]["reason"]


def test_malformed_id_is_still_rejected():
    out = _check("nonsense")
    assert out["rejected"] == ["nonsense"]
    assert out["unknown"] == []
    assert out["rows"] == []


def test_revoked_id_is_unknown_and_names_its_replacement():
    out = _check("T1002")  # Data Compressed, revoked in favour of T1560
    assert out["gaps"] == []
    assert out["unknown"] == [
        {"technique": "T1002", "reason": "revoked; replaced by T1560"}
    ]


def test_mixed_ids_and_case_and_repeats():
    out = _check("t1059", "T1059", "T9999", "t9999", "T1002", "bad")
    assert out["checked"] == 1
    assert out["gaps"] == ["T1059"]
    assert [u["technique"] for u in out["unknown"]] == ["T9999", "T1002"]
    assert out["rejected"] == ["bad"]


def test_deprecated_id_is_unknown():
    from core.threat_intel.mitre_lookup import attack_techniques

    deprecated = next(
        k for k, v in attack_techniques().items() if v["status"] == "deprecated"
    )
    assert _check(deprecated)["unknown"] == [
        {"technique": deprecated, "reason": "deprecated"}
    ]


@pytest.mark.asyncio
async def test_identify_gaps_keeps_unknown_out_of_gaps_and_answer(monkeypatch):
    from core.detections import tools

    monkeypatch.setattr(tools, "read_ledger", lambda *_: [])
    monkeypatch.setattr(
        tools,
        "read_alert_labels",
        lambda _c: gaps.AlertLabels(scope="all alerts", total=0),
    )
    monkeypatch.setattr(tools, "DatabaseDataService", lambda: object())
    monkeypatch.setattr(catalog, "rules_by_technique", lambda: {"T1059": {1}})
    monkeypatch.setattr(catalog, "rules_by_tactic", lambda: {})
    monkeypatch.setattr(catalog, "rule_counts", lambda: {"sigma": 1})
    monkeypatch.setattr(
        catalog,
        "siem_checks_by_technique",
        lambda: {"T1059": {"rules": 1, "proven": 1}},
    )
    monkeypatch.setattr(catalog, "blind_spots", lambda: [])
    monkeypatch.setattr(catalog, "rule_health", lambda: {})

    out = await SecurityDetectionsTools().identify_gaps(
        techniques=["T1059", "T9999", "T1002", "nonsense"]
    )
    checked = out["techniques_checked"]
    assert checked["rows"][0]["bucket"] == "proven"
    assert checked["gaps"] == []
    assert {u["technique"] for u in checked["unknown"]} == {"T9999", "T1002"}
    assert out["answer"] != "gaps_found"
    assert out["gaps_identified"] == 0


def test_lookup_resolves_a_name_the_old_44_id_list_lacked():
    # T1218.011 (Rundll32) was never in the hand-kept list.
    assert resolve_technique("T1218.011") == ("T1218.011", "Rundll32", "Stealth")


def test_lookup_keeps_names_it_resolved_before():
    assert resolve_technique("T1059.001")[1:] == ("PowerShell", "Execution")
    assert resolve_technique("T1003")[1:] == (
        "OS Credential Dumping",
        "Credential Access",
    )


def test_lookup_falls_back_to_the_parent_then_the_id():
    assert resolve_technique("T1059.999")[1] == "Command and Scripting Interpreter"
    assert resolve_technique("T9999") == ("T9999", "T9999", "Unknown")


def _verdict(techniques, outcome="proven"):
    from datetime import datetime

    return {
        "investigation_kind": "hunt",
        "investigation_id": "h1",
        "hypothesis_id": "hyp",
        "outcome": outcome,
        "trust": "analyst",
        "techniques": set(techniques),
        "subjects": [],
        "first_seen": datetime(2026, 11, 1),
        "last_seen": datetime(2026, 11, 1, 1),
        "concluded_at": datetime(2026, 11, 2),
        "entities": {"hostnames": ["h"], "src_ips": [], "users": []},
    }


def test_missed_runs_never_list_ids_that_are_not_active_techniques():
    report = gaps.missed_runs_report(
        "",
        False,
        [
            _verdict({"T1059", "T9999", "T1002"}),
            _verdict({"T9999", "T1002"}, outcome="disproven"),
        ],
        lambda start, end: [],
        {},
        30,
    )
    assert [g["technique"] for g in report["groups"]] == ["T1059"]
    assert report["groups"][0]["ruled_out"] == []
    assert "T9999" not in str(report) and "T1002" not in str(report)


def test_revoked_chain_names_the_final_active_id():
    out = _check("T1073")  # revoked -> T1574.002, itself revoked -> T1574.001
    assert out["unknown"] == [
        {"technique": "T1073", "reason": "revoked; replaced by T1574.001"}
    ]


def test_revoked_id_is_marked_revoked_where_its_name_is_shown():
    assert resolve_technique("T1073")[1] == "DLL Side-Loading (revoked: use T1574.001)"
