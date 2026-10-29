"""What-if replay of approval decisions under declared intent (#1064).

The pure functions and the diff need no Postgres. ``main`` without ``--replay``
keeps the observe-mode report.
"""

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from core.intent import IntentDiff, format_rows
from core.response.config import ResponseConfig, approval_requirement
from services.daemon.config import DaemonConfig
from services.daemon.intent import (
    ReplayApproval,
    declared_daemon_config,
    effective_daemon_config,
    format_replay,
    main,
    parse_since,
    replay_decisions,
)

pytestmark = pytest.mark.unit


def _approval(action_id, confidence, reversibility="reversible"):
    return ReplayApproval(action_id, confidence, reversibility)


# --- hoisted decisions -------------------------------------------------------


def test_approval_requirement_matches_the_live_branch():
    config = ResponseConfig()
    assert approval_requirement(False, "reversible", 0.87, config) == (
        True,
        "effect_class=reversible",
    )
    assert approval_requirement(False, "reversible", 0.99, config) == (
        True,
        "effect_class=reversible",
    )
    held, rule = approval_requirement(
        False, "reversible", 0.92, ResponseConfig(confidence_threshold=0.95)
    )
    assert held is True
    assert rule == "effect_class=reversible"
    assert approval_requirement(False, "irreversible", 0.99, config) == (
        True,
        "effect_class=irreversible",
    )
    assert approval_requirement(False, None, 0.99, config) == (
        True,
        "effect_class=unclassified",
    )
    assert approval_requirement(True, "sideways", 0.99, config)[0] is True


def test_unknown_effect_class_raises():
    with pytest.raises(ValueError, match="Unknown effect class"):
        approval_requirement(False, "sideways", 0.9, ResponseConfig())


# --- diff of two outcome sets ------------------------------------------------


def _seeded():
    return [
        _approval("a-87", 0.87),
        _approval("a-92", 0.92),
        _approval("a-96", 0.96),
    ]


def test_equal_configs_differ_on_nothing():
    report = replay_decisions(_seeded(), ResponseConfig(), ResponseConfig())
    assert report.rows == []
    text = "\n".join(format_replay(report))
    assert "0 actions would have required approval that did not" in text
    assert "0 would have executed unattended that waited" in text


def test_a_raised_threshold_does_not_release_or_hold_an_approval():
    report = replay_decisions(
        _seeded(),
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.95),
    )
    assert report.rows == []
    assert report.actions_now_requiring_approval == 0
    assert report.actions_now_unattended == 0


def test_loosened_threshold_does_not_run_unattended():
    report = replay_decisions(
        [_approval("a-87", 0.87)],
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.80),
    )
    assert report.actions_now_unattended == 0
    assert report.actions_now_requiring_approval == 0
    assert report.rows == []


def test_same_action_under_a_different_rule_is_not_a_row():
    report = replay_decisions(
        [_approval("a-96", 0.96)],
        ResponseConfig(),
        ResponseConfig(confidence_threshold=0.95),
    )
    assert report.rows == []


# --- declared overlay --------------------------------------------------------


class _FakeConfigService:
    def __init__(self, rows):
        self.rows = rows

    def read_system_config(self, key):
        return self.rows.get(key)


def test_effective_act_flag_uses_the_ceiling_and_declared_can_override(monkeypatch):
    svc = _FakeConfigService(
        {
            "autonomy.ceiling": {
                "act_reversible": True,
                "act_irreversible": False,
                "four_eyes": True,
                "stopped": False,
            }
        }
    )
    monkeypatch.setattr("core.storage.config_service.get_config_service", lambda: svc)
    base = DaemonConfig()
    effective = effective_daemon_config(base)
    assert base.ceiling.act_reversible is False
    assert effective.ceiling.act_reversible is True
    assert effective.ceiling.four_eyes is True
    declared = declared_daemon_config(effective, {"act.reversible": False})
    assert declared.ceiling.act_reversible is False
    assert effective.ceiling.act_reversible is True


class _BrokenConfigService:
    def read_system_config(self, key):
        raise RuntimeError("db down")


def test_failed_ceiling_read_does_not_raise_an_act_flag(monkeypatch):
    svc = _BrokenConfigService()
    monkeypatch.setattr("core.storage.config_service.get_config_service", lambda: svc)
    effective = effective_daemon_config(DaemonConfig())
    assert effective.ceiling.act_reversible is False
    assert effective.ceiling.act_irreversible is False


def test_wrong_type_is_skipped_and_a_sibling_key_still_applies(caplog):
    caplog.set_level(logging.WARNING)
    declared = declared_daemon_config(
        DaemonConfig(),
        {
            "respond.confidence_threshold": "0.95",
            "respond.review_threshold": 0.75,
        },
    )
    assert declared.response.confidence_threshold == 0.90
    assert declared.response.review_threshold == 0.75
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
