"""INTENT.md observe mode (#915): loader, label rule, diff, sources. No Postgres."""

import dataclasses
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import Settings, get_settings
from core.intent import (
    DEFAULT_INTENT_FILE,
    HIGHER_TIGHTER,
    INTENT_FIELDS,
    LOWER_TIGHTER,
    SHORTER_TIGHTER,
    diff_intent,
    effective_values,
    label,
    read_intent,
)
from core.response.config import ResponseConfig
from services.daemon.config import DaemonConfig
from services.daemon.intent import intent_report, report_intent

pytestmark = pytest.mark.unit


def _no_db(*_a, **_k):
    raise RuntimeError("no database in unit tests")


@pytest.fixture
def offline_config():
    # Both the lazy import inside from_env() and the module-level one in
    # services.daemon.intent, so no test here reaches for Postgres.
    with patch("core.storage.config_service.get_config_service", _no_db), patch(
        "services.daemon.intent.get_config_service", _no_db
    ):
        yield


def _intent_records(caplog):
    return [r for r in caplog.records if r.name.endswith("intent")]


def _write(tmp_path: Path, frontmatter: str) -> Path:
    path = tmp_path / "INTENT.md"
    path.write_text(f"---\n{frontmatter}\n---\n\nprose body\n", encoding="utf-8")
    return path


# --- shipped manifest --------------------------------------------------------


def test_shipped_manifest_declares_every_field_at_default():
    declared = read_intent(DEFAULT_INTENT_FILE)
    assert declared is not None
    assert set(declared) == {f.key for f in INTENT_FIELDS}
    assert diff_intent(declared, effective_values(DaemonConfig()), {}) == []


def test_every_response_knob_has_a_manifest_key():
    # dry_run only suppresses execution; it is not an autonomy knob.
    paths = {f.path for f in INTENT_FIELDS}
    missing = (
        {f"response.{f.name}" for f in dataclasses.fields(ResponseConfig)}
        - {"response.dry_run"}
        - paths
    )
    assert not missing, f"ResponseConfig fields without an INTENT.md key: {missing}"


def test_every_field_names_a_real_setting():
    # A typo here would silently report the key's source as "default" forever.
    assert {f.setting for f in INTENT_FIELDS} <= set(Settings.model_fields)


def test_fresh_checkout_reports_no_differences(offline_config, caplog, monkeypatch):
    for name in list(os.environ):
        if name.upper().startswith(("DAEMON_", "ORCHESTRATOR_")):
            monkeypatch.delenv(name)
    caplog.set_level(logging.INFO)
    report_intent(DaemonConfig.from_env())
    records = _intent_records(caplog)
    assert not [r for r in records if r.levelno >= logging.WARNING]
    assert any("no declared key differs" in r.message for r in records)


# --- label rule, both directions per field type -------------------------------


@pytest.mark.parametrize(
    "rule, declared, effective, expected",
    [
        (HIGHER_TIGHTER, 0.95, 0.90, "tighten"),
        (HIGHER_TIGHTER, 0.85, 0.90, "loosen"),
        (SHORTER_TIGHTER, ["critical"], ["critical", "high"], "tighten"),
        (
            SHORTER_TIGHTER,
            ["critical", "high", "medium"],
            ["critical", "high"],
            "loosen",
        ),
        (LOWER_TIGHTER, 2.0, 5.0, "tighten"),
        (LOWER_TIGHTER, 10.0, 5.0, "loosen"),
        (LOWER_TIGHTER, False, True, "tighten"),
        (LOWER_TIGHTER, True, False, "loosen"),
        (HIGHER_TIGHTER, True, False, "tighten"),  # force_manual_approval
        (HIGHER_TIGHTER, False, True, "loosen"),
        (LOWER_TIGHTER, 0.9, 0.9, "same"),
        (SHORTER_TIGHTER, ["high", "critical"], ["critical", "high"], "same"),
        (SHORTER_TIGHTER, ["critical", "medium"], ["critical", "high"], "same"),
    ],
)
def test_label(rule, declared, effective, expected):
    assert label(rule, declared, effective) == expected


def test_reordered_or_recased_severities_are_not_a_difference():
    config = DaemonConfig()
    declared = {"escalate.severities": ["HIGH", "critical", "high"]}
    assert diff_intent(declared, effective_values(config), {}) == []


@pytest.mark.parametrize(
    "key, bad",
    [
        ("triage.auto_triage", 1),
        ("respond.confidence_threshold", "0.95"),
        ("escalate.severities", "critical"),
        ("investigate.enabled", None),
    ],
)
def test_wrong_type_is_one_warning_and_no_row(key, bad, caplog):
    caplog.set_level(logging.WARNING)
    rows = diff_intent({key: bad}, effective_values(DaemonConfig()), {})
    assert rows == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and key in warnings[0].message


# --- diff ---------------------------------------------------------------------


def test_diff_reports_only_differing_keys_with_source_and_label():
    config = DaemonConfig()
    config.response.confidence_threshold = 0.80
    declared = {"respond.confidence_threshold": 0.90, "triage.auto_triage": True}
    rows = diff_intent(
        declared,
        effective_values(config),
        {"response.confidence_threshold": "env"},
    )
    assert [(r.key, r.declared, r.effective, r.source, r.label) for r in rows] == [
        ("respond.confidence_threshold", 0.90, 0.80, "env", "tighten")
    ]


def test_diff_source_defaults_when_unrecorded():
    config = DaemonConfig()
    rows = diff_intent({"investigate.enabled": True}, effective_values(config), {})
    assert rows[0].source == "default" and rows[0].label == "loosen"


def test_include_same_emits_equal_keys_without_changing_the_default():
    config = DaemonConfig()
    config.response.confidence_threshold = 0.80
    declared = {"respond.confidence_threshold": 0.90, "triage.auto_triage": True}
    sources = {"response.confidence_threshold": "env"}
    effective = effective_values(config)
    assert [(r.key, r.label) for r in diff_intent(declared, effective, sources)] == [
        ("respond.confidence_threshold", "tighten")
    ]
    assert [
        (r.key, r.declared, r.effective, r.source, r.label)
        for r in diff_intent(declared, effective, sources, include_same=True)
    ] == [
        ("triage.auto_triage", True, True, "default", "same"),
        ("respond.confidence_threshold", 0.90, 0.80, "env", "tighten"),
    ]


def test_intent_report_include_same_lists_every_key(offline_config, monkeypatch):
    for name in list(os.environ):
        if name.upper().startswith(("DAEMON_", "ORCHESTRATOR_")):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    rows = intent_report(include_same=True)
    assert rows is not None
    assert [r.key for r in rows] == [f.key for f in INTENT_FIELDS]
    assert {r.label for r in rows} == {"same"}
    # Log and CLI keep the differing-rows-only report.
    assert intent_report() == []


# --- sources recorded by from_env ---------------------------------------------


def test_from_env_records_env_and_default_sources(offline_config, monkeypatch):
    monkeypatch.setenv("DAEMON_CONFIDENCE_THRESHOLD", "0.75")
    config = DaemonConfig.from_env()
    assert config.sources["response.confidence_threshold"] == "env"
    assert config.sources["processing.auto_triage_enabled"] == "default"
    assert config.response.confidence_threshold == 0.75


def test_from_env_threads_triage_timeout(offline_config, monkeypatch):
    monkeypatch.delenv("DAEMON_TRIAGE_TIMEOUT", raising=False)
    assert DaemonConfig.from_env().processing.triage_timeout == 60
    monkeypatch.setenv("DAEMON_TRIAGE_TIMEOUT", "150")
    get_settings.cache_clear()
    assert DaemonConfig.from_env().processing.triage_timeout == 150


class _FakeConfigService:
    def __init__(self, rows):
        self.rows = rows

    def get_system_config(self, key, default=None):
        return self.rows.get(key, default)


def test_db_overlays_report_source_db(tmp_path, monkeypatch):
    svc = _FakeConfigService(
        {
            "orchestrator.settings": {"max_cost_per_investigation": 2.5},
            "approval.force_manual_approval": {"enabled": True},
        }
    )
    monkeypatch.setattr("core.storage.config_service.get_config_service", lambda: svc)
    monkeypatch.setattr("services.daemon.intent.get_config_service", lambda: svc)
    path = _write(
        tmp_path,
        "respond:\n  force_manual_approval: false\n"
        "investigate:\n  max_cost_per_investigation: 5.0\n",
    )
    rows = {r.key: r for r in intent_report(path=path)}
    cost = rows["investigate.max_cost_per_investigation"]
    assert (cost.effective, cost.source, cost.label) == (2.5, "db", "loosen")
    force = rows["respond.force_manual_approval"]
    assert (force.effective, force.source, force.label) == (True, "db", "loosen")


# --- file problems: one warning, daemon still starts --------------------------


def test_missing_file_is_one_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    assert read_intent(tmp_path / "absent.md") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "absent.md" in warnings[0].message


def test_malformed_frontmatter_is_one_warning_naming_the_file(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    path = _write(tmp_path, ": [not yaml")
    assert read_intent(path) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(path) in warnings[0].message


def test_manifest_declaring_nothing_is_one_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    path = _write(tmp_path, "# only comments")
    assert read_intent(path) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(path) in warnings[0].message


def test_unknown_key_is_dropped_with_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    path = _write(tmp_path, "respond:\n  nobody_reads_this: 1\n  auto_response: true")
    assert read_intent(path) == {"respond.auto_response": True}
    assert any("nobody_reads_this" in r.message for r in caplog.records)


def test_report_never_raises(caplog):
    caplog.set_level(logging.WARNING)
    with patch("services.daemon.intent.read_intent", side_effect=RuntimeError("boom")):
        report_intent(DaemonConfig())
    assert any("non-fatal" in r.message for r in caplog.records)
