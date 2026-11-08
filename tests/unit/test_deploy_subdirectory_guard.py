"""A deploy target's subdirectory goes into the deploy PR's file path, which is
sent to the Git host with the token. One that climbs out of the repo ('..') or
is absolute is refused when the target is saved, and again before a deploy so a
hand-edited detection_sources.json sends no request at all."""

from datetime import datetime, timezone

import httpx
import pytest

from core.detections.detection_rules_service import (
    DetectionRulesService,
    deploy_subdirectory_refused,
)
from core.detections.inventory import rollout

URL = "https://github.com/acme/rules.git"
BAD = [
    "..",
    "../",
    "../../etc",
    "rules/../..",
    "rules/../../etc",
    "./rules",
    "rules/./win",
    "rules//win",
    "//rules",
    "rules\\win",
    "\\rules",
    "C:/rules",
    "C:\\rules",
    "rules win",
    "rules%2e%2e",
    "rules?x=1",
    "rules#x",
    "rules\x00",
    "rules\n",
]
GOOD = ["", "rules", "rules/windows", "/rules/", "a/b.c/d_e-f", "rules/"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = DetectionRulesService.__new__(DetectionRulesService)
    svc.base_dir = tmp_path
    svc._overlay_removed = False
    svc.sources = []
    monkeypatch.setattr(svc, "_save_config", lambda: None)
    monkeypatch.setattr(svc, "_git_clone", lambda url, path: None)
    monkeypatch.setattr(svc, "_count_rules", lambda *a: 0)
    return svc


def _source(svc, sub):
    svc.sources = [
        {
            "id": "s1",
            "name": "ours",
            "type": "git",
            "format": "elastic",
            "git_url": URL,
            "local_path": "/nowhere/s1",
            "subdirectory": sub,
            "deploy_target": False,
        }
    ]
    return svc.sources[0]


@pytest.mark.parametrize("sub", BAD)
def test_a_bad_subdirectory_is_refused_when_the_target_is_set(service, sub):
    source = _source(service, sub)
    with pytest.raises(ValueError, match="subdirectory"):
        service.set_deploy_target("s1", True)
    assert not source["deploy_target"]


@pytest.mark.parametrize("sub", BAD)
def test_a_bad_subdirectory_is_refused_when_the_target_is_added(service, sub):
    with pytest.raises(ValueError):
        service.add_source(
            name="ours",
            source_type="git",
            format="elastic",
            url=URL,
            subdirectory=sub,
            deploy_target=True,
        )
    assert service.sources == []


@pytest.mark.parametrize("sub", GOOD)
def test_a_good_subdirectory_is_accepted(service, sub):
    source = _source(service, sub)
    service.set_deploy_target("s1", True)
    assert source["deploy_target"]
    assert deploy_subdirectory_refused({"subdirectory": sub}) == ""


def test_a_missing_subdirectory_is_accepted():
    assert deploy_subdirectory_refused({}) == ""
    assert deploy_subdirectory_refused({"subdirectory": None}) == ""


def _copy():
    return rollout.Copy(
        rule_id=7,
        version=1,
        name="rule 7",
        language="elastic",
        stage="live",
        stage_at=datetime.now(timezone.utc),
        checks={},
        rule={},
        key="key-7",
        toml='[rule]\nname = "rule 7"\n',
        path="7.toml",
    )


@pytest.fixture
def wire(monkeypatch):
    """Every httpx client goes to a transport that records the requests."""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500, json={"message": "no github here"})

    real = httpx.Client

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "Client", client)
    return requests


@pytest.mark.parametrize("sub", ["../", "../../etc", "rules/..", "/../x"])
def test_open_pr_sends_nothing_for_a_bad_subdirectory(wire, sub):
    target = {"git_url": URL, "subdirectory": sub}
    with pytest.raises(RuntimeError, match="subdirectory"):
        rollout._open_pr(_copy(), target, "body", "ghp_secret")
    assert wire == []


@pytest.mark.parametrize("sub", ["../", "../../etc"])
def test_deploy_records_the_refusal_and_sends_nothing(wire, monkeypatch, sub):
    from contextlib import contextmanager

    import core.secrets

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    outcomes = []
    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(
        rollout, "_record_deploy", lambda c, out, actor="system": outcomes.append(out)
    )
    monkeypatch.setattr(core.secrets, "get_secret", lambda k, default=None: "ghp_x")
    rollout._deploy(
        [_copy()],
        {"elastic": {"git_url": URL, "subdirectory": sub, "deploy_target": True}},
        datetime.now(timezone.utc),
        rollout.RolloutReport(),
    )
    assert wire == []
    assert "subdirectory" in outcomes[0]["error"]
