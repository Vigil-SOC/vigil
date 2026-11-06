"""At most one deploy target per format: a second repo of that format is refused
until the first is cleared, and the source list never shows two as on."""

import pytest

from core.detections.detection_rules_service import DetectionRulesService
from services.api.routers.detection_rules import list_sources

VENDOR_ELASTIC = "https://github.com/elastic/detection-rules.git"


def _git(sid, fmt, url, target=False):
    return {
        "id": sid,
        "name": f"source {sid}",
        "type": "git",
        "format": fmt,
        "git_url": url,
        "status": "not_cloned",
        "local_path": f"/nowhere/{sid}",
        "subdirectory": "",
        "deploy_target": target,
    }


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = DetectionRulesService.__new__(DetectionRulesService)
    svc.base_dir = tmp_path
    svc._overlay_removed = False
    svc.sources = [
        _git("own-e1", "elastic", "https://github.com/acme/detections.git", True),
        _git("own-e2", "elastic", "https://github.com/acme/more-detections.git"),
        _git("own-s1", "splunk", "https://github.com/acme/searches.git"),
        _git("sigma", "sigma", "https://github.com/acme/sigma.git"),
        _git("vendor-e", "elastic", VENDOR_ELASTIC),
    ]
    monkeypatch.setattr(svc, "_save_config", lambda: None)
    return svc


def _on(svc):
    return {s["id"] for s in svc.sources if s.get("deploy_target")}


def test_a_second_target_of_one_format_is_refused(service):
    with pytest.raises(ValueError, match="already the elastic deploy target"):
        service.set_deploy_target("own-e2", True)
    assert _on(service) == {"own-e1"}


def test_clearing_the_first_lets_another_take_over(service):
    service.set_deploy_target("own-e1", False)
    service.set_deploy_target("own-e2", True)
    assert _on(service) == {"own-e2"}


def test_each_format_has_its_own_target(service):
    service.set_deploy_target("own-s1", True)
    assert _on(service) == {"own-e1", "own-s1"}


def test_setting_the_current_target_again_is_fine(service):
    service.set_deploy_target("own-e1", True)
    assert _on(service) == {"own-e1"}


def test_adding_a_second_target_of_one_format_is_refused(service):
    before = list(service.sources)
    with pytest.raises(ValueError, match="already the elastic deploy target"):
        service.add_source(
            name="Third",
            source_type="git",
            format="elastic",
            url="https://github.com/acme/third.git",
            deploy_target=True,
        )
    assert service.sources == before


@pytest.mark.parametrize(
    "sid, reason",
    [("sigma", "splunk or elastic"), ("vendor-e", "vendor's public repo")],
)
def test_formats_and_vendor_repos_that_cannot_be_targets(service, sid, reason):
    service.set_deploy_target("own-e1", False)
    with pytest.raises(ValueError, match=reason):
        service.set_deploy_target(sid, True)


def test_the_list_shows_only_the_target_rollout_honours(service):
    # A hand-edited file can hold two flags of one format, and a vendor's.
    for s in service.sources:
        if s["id"] in ("own-e2", "vendor-e"):
            s["deploy_target"] = True
    listed = {s["id"]: s for s in list_sources(service=service)["sources"]}
    assert [sid for sid, s in listed.items() if s["deploy_target"]] == ["own-e1"]
    assert listed["vendor-e"]["vendor_repo"] is True
    assert listed["own-e2"]["vendor_repo"] is False
