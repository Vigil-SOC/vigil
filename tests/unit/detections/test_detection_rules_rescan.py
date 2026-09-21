"""DetectionRulesService re-checks sources against disk on load (#968)."""

import json
from pathlib import Path

import pytest

from core.detections import detection_rules_service as drs
from core.detections.detection_rules_service import (
    DEFAULT_SOURCES,
    DetectionRulesService,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the service's home and state directory at a temp dir."""
    monkeypatch.setattr(drs, "_safe_home", lambda: tmp_path)

    def fake_vigil_path(*parts, write=False):
        target = tmp_path.joinpath(".vigil", *parts)
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(drs, "vigil_path", fake_vigil_path)
    return tmp_path


def _stale_config(home: Path) -> Path:
    """A config as written by a first boot before anything was cloned."""
    base = home / "security-detections"
    sources = [
        {
            "id": f"src{i}",
            "name": d["name"],
            "type": d["type"],
            "git_url": d["git_url"],
            "format": d["format"],
            "subdirectory": d.get("subdirectory", ""),
            "story_subdirectory": d.get("story_subdirectory", ""),
            "clone_name": d["clone_name"],
            "local_path": str(base / d["clone_name"]),
            "rule_count": 0,
            "last_updated": None,
            "status": "not_cloned",
        }
        for i, d in enumerate(DEFAULT_SOURCES)
    ]
    config = home / ".vigil" / "detection_sources.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"sources": sources, "version": 1}))
    return config


def test_load_rescans_stale_config_against_disk(home):
    config = _stale_config(home)
    rules = home / "security-detections" / "sigma" / "rules"
    rules.mkdir(parents=True)
    (rules / "x.yml").write_text("title: x\n")

    service = DetectionRulesService()

    by_name = {s["name"]: s for s in service.list_sources()}
    assert by_name["Sigma Rules"]["status"] == "ready"
    assert by_name["Sigma Rules"]["rule_count"] == 1
    assert by_name["Splunk ESCU"]["status"] == "not_cloned"

    stats = service.get_stats()
    assert stats["total_rules"] == 1
    assert stats["sources_count"] == 4

    assert service.get_mcp_env_vars() == {"SIGMA_PATHS": str(rules)}

    on_disk = {s["name"]: s for s in json.loads(config.read_text())["sources"]}
    assert on_disk["Sigma Rules"]["status"] == "ready"
    assert on_disk["Sigma Rules"]["rule_count"] == 1


def test_rescan_does_not_rewrite_unchanged_config(home):
    config = _stale_config(home)
    before = config.stat().st_mtime_ns

    DetectionRulesService()

    assert config.stat().st_mtime_ns == before


def test_malformed_entry_does_not_reset_to_defaults(home):
    config = _stale_config(home)
    data = json.loads(config.read_text())
    data["sources"].append({"id": "odd", "name": "Odd", "format": "sigma"})
    config.write_text(json.dumps(data))

    service = DetectionRulesService()

    assert [s["id"] for s in service.list_sources()][-1] == "odd"
    assert len(service.list_sources()) == 5


def test_rescan_marks_missing_local_source_as_error(home):
    _stale_config(home)
    service = DetectionRulesService()
    gone = home / "custom-rules"
    gone.mkdir()
    service.add_source("Custom", "local", "sigma", path=str(gone))
    gone.rmdir()

    service.rescan_sources()

    custom = next(s for s in service.list_sources() if s["name"] == "Custom")
    assert custom["status"] == "error"
    assert custom["rule_count"] == 0
