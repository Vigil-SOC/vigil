"""What-if replay of response decisions under declared intent (#1064).

The pure functions and the diff need no Postgres. ``main`` without ``--replay``
keeps the observe-mode report.
"""

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from core.intent import IntentDiff, format_rows
from core.response.approval_service import Reversibility
from core.response.config import (
    ResponseConfig,
    approval_requirement,
    response_action_decision,
)
from services.daemon.config import DaemonConfig
from services.daemon.intent import (
    ReplayApproval,
    ReplayFinding,
    declared_daemon_config,
    effective_daemon_config,
    format_replay,
    main,
    parse_since,
    replay_decisions,
)

pytestmark = pytest.mark.unit


def _finding(finding_id, severity, confidence, recommended=""):
    return ReplayFinding(finding_id, severity, confidence, recommended)


def _approval(action_id, confidence, reversibility=Reversibility.REVERSIBLE):
    return ReplayApproval(action_id, confidence, reversibility)


# --- hoisted decisions -------------------------------------------------------


def test_response_action_matches_the_live_branches():
    config = ResponseConfig()
    assert response_action_decision("critical", 0.70, "", config) == (
        "isolate",
        "response.critical_action_floor=0.70 met (0.70)",
    )
    assert response_action_decision("critical", 0.69, "", config) is None
    assert response_action_decision("high", 0.80, "", config)[0] == "investigate"
    assert response_action_decision("medium", 0.92, "isolate", config) == (
        "isolate",
        "response.confidence_threshold=0.90 met (0.92)",
    )
    assert response_action_decision("medium", 0.92, "block", config)[0] == "block"


def test_auto_response_off_is_no_action():
    config = ResponseConfig(auto_response_enabled=False)
    assert response_action_decision("critical", 0.99, "isolate", config) is None


def test_approval_requirement_matches_the_live_branch():
    config = ResponseConfig()
    assert approval_requirement(False, Reversibility.REVERSIBLE, 0.87, config) == (
        True,
        "response.confidence_threshold=0.90 not met (0.87)",
    )
    assert approval_requirement(False, Reversibility.REVERSIBLE, 0.92, config) == (
        False,
        "response.confidence_threshold=0.90 met (0.92)",
    )
    held, rule = approval_requirement(
        False, Reversibility.REVERSIBLE, 0.92, ResponseConfig(confidence_threshold=0.95)
    )
    assert held is True
    assert rule == "response.confidence_threshold=0.95 not met (0.92)"
    assert approval_requirement(False, Reversibility.IRREVERSIBLE, 0.99, config) == (
        True,
        "reversibility=irreversible",
    )
    assert approval_requirement(True, "sideways", 0.99, config)[0] is True


def test_unknown_reversibility_raises():
    with pytest.raises(ValueError, match="Unknown reversibility"):
        approval_requirement(False, "sideways", 0.9, ResponseConfig())


# --- diff of two outcome sets ------------------------------------------------


def _seeded():
    findings = [_finding("f-crit", "critical", 0.72)]
    approvals = [
        _approval("a-87", 0.87),
        _approval("a-92", 0.92),
        _approval("a-96", 0.96),
    ]
    return findings, approvals


def test_equal_configs_differ_on_nothing():
    findings, approvals = _seeded()
    report = replay_decisions(findings, approvals, ResponseConfig(), ResponseConfig())
    assert report.rows == []
    text = "\n".join(format_replay(report))
    assert "0 actions would have required approval that did not" in text
    assert "0 would have executed unattended that waited" in text
    assert "0 findings would have gained or lost a response action" in text


def test_raised_threshold_holds_only_the_confidence_that_crosses():
    # 0.87 already waits at 0.90, and 0.96 still clears 0.95. Only 0.92 flips.
    findings, approvals = _seeded()
    report = replay_decisions(
        findings,
        approvals,
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.95),
    )
    assert [row.id for row in report.rows] == ["a-92"]
    row = report.rows[0]
    assert row.effective == "unattended"
    assert row.declared == "approval"
    assert row.effective_rule == "response.confidence_threshold=0.90 met (0.92)"
    assert row.declared_rule == "response.confidence_threshold=0.95 not met (0.92)"
    assert report.actions_now_requiring_approval == 1
    assert report.actions_now_unattended == 0
    assert report.findings_gained_or_lost == 0


def test_raised_critical_floor_drops_the_finding_action():
    findings, approvals = _seeded()
    report = replay_decisions(
        findings,
        approvals,
        ResponseConfig(),
        ResponseConfig(critical_action_floor=0.75),
    )
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.kind == "finding" and row.id == "f-crit"
    assert row.effective == "isolate"
    assert row.declared == "none"
    assert row.effective_rule == "response.critical_action_floor=0.70 met (0.72)"
    assert report.findings_gained_or_lost == 1


def test_auto_response_off_reports_the_finding_losing_its_action():
    report = replay_decisions(
        [_finding("f-1", "medium", 0.92, "isolate")],
        [],
        ResponseConfig(),
        ResponseConfig(auto_response_enabled=False),
    )
    assert report.rows[0].effective == "isolate"
    assert report.rows[0].declared == "none"
    assert report.findings_gained_or_lost == 1


def test_loosened_threshold_would_have_run_unattended():
    report = replay_decisions(
        [],
        [_approval("a-87", 0.87)],
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.80),
    )
    assert report.actions_now_unattended == 1
    assert report.actions_now_requiring_approval == 0
    assert report.rows[0].declared == "unattended"


def test_same_action_under_a_different_rule_is_not_a_row():
    # 0.96 still auto-approves at 0.95; the rule text changes, the outcome does not.
    report = replay_decisions(
        [],
        [_approval("a-96", 0.96)],
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.95),
    )
    assert report.rows == []


# --- declared overlay --------------------------------------------------------


class _FakeConfigService:
    def __init__(self, rows):
        self.rows = rows

    def get_system_config(self, key, default=None):
        return self.rows.get(key, default)


def test_effective_force_uses_the_db_overlay_and_declared_can_override(monkeypatch):
    svc = _FakeConfigService({"approval.force_manual_approval": {"enabled": True}})
    monkeypatch.setattr("services.daemon.intent.get_config_service", lambda: svc)
    base = DaemonConfig()
    effective = effective_daemon_config(base)
    assert base.response.force_manual_approval is False
    assert effective.response.force_manual_approval is True
    declared = declared_daemon_config(
        effective, {"respond.force_manual_approval": False}
    )
    assert declared.response.force_manual_approval is False
    assert effective.response.force_manual_approval is True


def test_wrong_type_is_skipped_and_a_sibling_key_still_applies(caplog):
    caplog.set_level(logging.WARNING)
    declared = declared_daemon_config(
        DaemonConfig(),
        {
            "respond.confidence_threshold": "0.95",
            "respond.critical_action_floor": 0.75,
        },
    )
    assert declared.response.confidence_threshold == 0.90
    assert declared.response.critical_action_floor == 0.75
    assert any("confidence_threshold" in r.message for r in caplog.records)


def test_parse_since():
    assert parse_since("7d") == timedelta(days=7)
    assert parse_since("24H") == timedelta(hours=24)
    assert parse_since("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_since("week")


def test_main_without_replay_is_the_observe_report(capsys, monkeypatch):
    monkeypatch.setattr("services.daemon.intent.intent_report", lambda *a, **k: [])
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "no declared key differs from effective daemon config" in out
    assert "summary:" not in out
    assert "--replay" not in out


def test_main_without_replay_prints_diffs_unchanged(capsys, monkeypatch):
    rows = [IntentDiff("respond.confidence_threshold", 0.95, 0.90, "env", "tighten")]
    monkeypatch.setattr("services.daemon.intent.intent_report", lambda *a, **k: rows)
    monkeypatch.setattr("services.daemon.intent.intent_file", lambda: Path("INTENT.md"))
    assert main([]) == 0
    expected = (
        "INTENT.md: 1 key(s) differ from effective daemon config\n"
        + "\n".join(format_rows(rows))
        + "\n"
    )
    assert capsys.readouterr().out == expected
