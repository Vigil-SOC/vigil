"""A stored subdirectory or story_subdirectory (the file can be hand-edited)
that leaves the source's clone yields no path, so nothing outside it is read."""

import os

import pytest

from core.detections.detection_rules_service import DetectionRulesService


@pytest.fixture
def clone(tmp_path):
    root = tmp_path / "clone"
    (root / "rules" / "win").mkdir(parents=True)
    (root / "stories").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    return root, outside


@pytest.fixture
def service(tmp_path):
    svc = DetectionRulesService.__new__(DetectionRulesService)
    svc.base_dir = tmp_path
    svc.sources = []
    svc.config_path = tmp_path / "none.json"
    return svc


def _src(root, sub="", story=""):
    return {
        "name": "s",
        "status": "ready",
        "format": "sigma",
        "local_path": str(root),
        "subdirectory": sub,
        "story_subdirectory": story,
    }


def _bad(root, outside):
    return ["../outside", str(outside), "link", "rules/../../outside"]


def test_escaping_subdirectories_yield_no_path_or_env(service, clone):
    root, outside = clone
    for sub in _bad(root, outside):
        src = _src(root, sub, sub)
        assert service._get_rules_path(src) is None
        assert service._get_story_path(src) is None
        assert service._mcp_env_vars([src]) == {}


def test_good_subdirectories_still_resolve(service, clone):
    root, _ = clone
    real = os.path.realpath(root)
    assert service._get_rules_path(_src(root)) == real
    assert service._get_rules_path(_src(root, "rules/win")) == f"{real}/rules/win"
    assert service._get_story_path(_src(root)) is None
    src = _src(root, "rules", "stories")
    assert service._get_story_path(src) == f"{real}/stories"
    env = service._mcp_env_vars([src])
    assert env["STORY_PATHS"] == f"{real}/stories"
    assert f"{real}/rules" in next(v for k, v in env.items() if k != "STORY_PATHS")
