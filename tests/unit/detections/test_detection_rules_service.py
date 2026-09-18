"""add_source is idempotent on (resolved rules directory, format) (#969)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from core.detections.detection_rules_service import DetectionRulesService


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("VIGIL_DIR", raising=False)
    with patch.object(Path, "home", return_value=tmp_path):
        yield DetectionRulesService()


@pytest.fixture
def rules_dir(tmp_path):
    rules = tmp_path / "myrules" / "rules"
    rules.mkdir(parents=True)
    for i in range(3):
        (rules / f"r{i}.yml").write_text("title: x\n")
    return rules


@pytest.mark.unit
def test_reregistering_same_directory_does_not_duplicate(service, rules_dir):
    base = rules_dir.parent
    before = len(service.sources)

    first = service.add_source(
        "Mine", "local", "sigma", path=str(base), subdirectory="rules"
    )
    stats_once = service.get_stats()
    second = service.add_source(
        "Mine again", "local", "sigma", path=str(base), subdirectory="rules"
    )

    assert second["id"] == first["id"]
    assert len(service.sources) == before + 1
    assert service.get_stats()["total_rules"] == stats_once["total_rules"] == 3
    assert service.get_stats()["sources_count"] == before + 1
    assert service.get_mcp_env_vars()["SIGMA_PATHS"] == str(rules_dir)


@pytest.mark.unit
def test_reregistering_seeded_git_default_clones_into_existing_entry(service):
    seeded = next(s for s in service.sources if s["clone_name"] == "sigma")
    assert seeded["status"] == "not_cloned"

    def fake_clone(url, target):
        (Path(target) / "rules").mkdir(parents=True)
        (Path(target) / "rules" / "a.yml").write_text("title: a\n")

    with patch.object(service, "_git_clone", side_effect=fake_clone) as clone:
        got = service.add_source(
            "Sigma again",
            "git",
            "sigma",
            url="https://github.com/SigmaHQ/sigma.git",
            subdirectory="rules",
        )

    clone.assert_called_once()
    assert got is seeded
    assert seeded["status"] == "ready" and seeded["rule_count"] == 1
    assert len(service.sources) == 4


@pytest.mark.unit
def test_equivalent_path_spellings_match(service, rules_dir):
    base = rules_dir.parent
    service.add_source("A", "local", "sigma", path=str(base), subdirectory="rules")
    # Same directory reached via the subdirectory folded into the path.
    dup = service.add_source("B", "local", "sigma", path=str(base / "rules"))
    assert dup["name"] == "A"
    assert [s["name"] for s in service.sources if s["type"] == "local"] == ["A"]


@pytest.mark.unit
def test_same_directory_different_format_is_distinct(service, rules_dir):
    base = rules_dir.parent
    service.add_source("Sigma", "local", "sigma", path=str(base), subdirectory="rules")
    other = service.add_source(
        "KQL", "local", "kql", path=str(base), subdirectory="rules"
    )
    assert other["name"] == "KQL"
    assert len([s for s in service.sources if s["type"] == "local"]) == 2
