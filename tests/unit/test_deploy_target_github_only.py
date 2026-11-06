"""A deploy target must be GitHub: github.com, or a host the person marked as
GitHub Enterprise. Anything else (GitLab, Bitbucket) is refused when it is set
or added, and again before a deploy, so the GitHub token never leaves for it."""

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import core.secrets
from core.detections.detection_rules_service import (
    GITHUB_ONLY,
    DetectionRulesService,
)
from core.detections.inventory import rollout

REFUSED = [
    "https://gitlab.com/acme/rules.git",
    "https://bitbucket.org/acme/rules.git",
    "git@gitlab.com:acme/rules.git",
    "ssh://git@bitbucket.org/acme/rules.git",
    "https://github.com@gitlab.com/acme/rules.git",
    "https://github.com.evil.example/acme/rules.git",
    "https://ghe.acme.internal/acme/rules.git",  # GHES, but not marked as such
]
GITHUB = [
    "https://github.com/acme/rules.git",
    "git@github.com:acme/rules.git",
    "ssh://git@github.com/acme/rules.git",
]
GHES = "https://ghe.acme.internal/acme/rules.git"


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = DetectionRulesService.__new__(DetectionRulesService)
    svc.base_dir = tmp_path
    svc._overlay_removed = False
    svc.sources = []
    monkeypatch.setattr(svc, "_save_config", lambda: None)
    return svc


def _source(svc, url, **extra):
    svc.sources = [
        {
            "id": "s1",
            "name": "ours",
            "type": "git",
            "format": "elastic",
            "git_url": url,
            "local_path": "/nowhere/s1",
            "subdirectory": "",
            "deploy_target": False,
            **extra,
        }
    ]
    return svc.sources[0]


@pytest.mark.parametrize("url", REFUSED)
def test_a_non_github_host_is_refused_when_set(service, url):
    source = _source(service, url)
    with pytest.raises(ValueError, match=GITHUB_ONLY.replace("(", r"\(").replace(")", r"\)")):
        service.set_deploy_target("s1", True)
    assert not source["deploy_target"]


@pytest.mark.parametrize("url", REFUSED[:4])
def test_a_non_github_host_is_refused_when_added(service, url):
    with pytest.raises(ValueError, match="only GitHub"):
        service.add_source(
            name="ours", source_type="git", format="elastic", url=url, deploy_target=True
        )
    assert service.sources == []


@pytest.mark.parametrize("url", GITHUB)
def test_github_com_is_accepted_as_is(service, url):
    source = _source(service, url)
    service.set_deploy_target("s1", True)
    assert source["deploy_target"]


def test_a_host_marked_github_enterprise_is_accepted_and_the_mark_kept(service):
    source = _source(service, GHES)
    service.set_deploy_target("s1", True, github_enterprise=True)
    assert source["deploy_target"] and source["github_enterprise"] is True
    # Turned off and on again without restating it: the saved mark holds.
    service.set_deploy_target("s1", False)
    service.set_deploy_target("s1", True)
    assert source["deploy_target"]


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/rules.git",
        "git@bitbucket.org:acme/rules.git",
        "https://dev.azure.com/acme/_git/rules",
        "https://acme.visualstudio.com/_git/rules",
    ],
)
def test_a_hosted_service_that_is_never_github_is_refused_even_marked(service, url):
    source = _source(service, url)
    with pytest.raises(ValueError, match="only GitHub"):
        service.set_deploy_target("s1", True, github_enterprise=True)
    assert not source["deploy_target"]


def test_deploy_ignores_a_ghes_mark_on_gitlab(deploy):
    seen = deploy(
        {"git_url": "https://gitlab.com/acme/rules.git", "deploy_target": True, "github_enterprise": True}
    )
    assert seen["clients"] == []
    assert GITHUB_ONLY in seen["outcomes"][0]["error"]


def test_saying_it_is_not_github_enterprise_is_refused(service):
    source = _source(service, GHES, github_enterprise=True)
    with pytest.raises(ValueError, match="only GitHub"):
        service.set_deploy_target("s1", True, github_enterprise=False)
    assert source["github_enterprise"] is True and not source["deploy_target"]


def test_a_host_marked_github_enterprise_can_be_added(service, monkeypatch):
    monkeypatch.setattr(service, "_git_clone", lambda url, path: None)
    monkeypatch.setattr(service, "_count_rules", lambda *a: 0)
    source = service.add_source(
        name="ours",
        source_type="git",
        format="elastic",
        url=GHES,
        deploy_target=True,
        github_enterprise=True,
    )
    assert source["deploy_target"] and source["github_enterprise"] is True


# --- the deploy itself ------------------------------------------------------


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
def deploy(monkeypatch):
    """Run _deploy with every outside call stubbed; record where HTTP would go."""
    seen = {"clients": [], "outcomes": []}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    class _Client:
        def __init__(self, base_url, headers, **_kw):
            seen["clients"].append((base_url, headers.get("Authorization")))

        def __enter__(self):
            raise RuntimeError("stop before any call")

        def __exit__(self, *exc):
            return False

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(rollout, "_stopped", lambda: False)

    def record(copy, out, actor="system"):
        seen["outcomes"].append(out)
        return out

    monkeypatch.setattr(rollout, "_record_deploy", record)
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: "ghp_secret")

    def run(target):
        rollout._deploy(
            [_copy()],
            {"elastic": target},
            datetime.now(timezone.utc),
            rollout.RolloutReport(),
        )
        return seen

    return run


@pytest.mark.parametrize("url", REFUSED)
def test_deploy_sends_nothing_to_an_unmarked_non_github_host(deploy, url):
    # A hand-edited sources file can mark one, so _deploy checks again.
    seen = deploy({"git_url": url, "deploy_target": True})
    assert seen["clients"] == []
    (outcome,) = seen["outcomes"]
    assert GITHUB_ONLY in outcome["error"]


def test_deploy_to_github_com_goes_to_api_github_com(deploy):
    seen = deploy({"git_url": GITHUB[1], "deploy_target": True})
    assert seen["clients"] == [("https://api.github.com", "Bearer ghp_secret")]


def test_deploy_to_a_marked_github_enterprise_host_goes_to_its_api(deploy):
    seen = deploy({"git_url": GHES, "deploy_target": True, "github_enterprise": True})
    assert seen["clients"] == [
        ("https://ghe.acme.internal/api/v3", "Bearer ghp_secret")
    ]


def test_open_pr_refuses_an_unmarked_host_whoever_calls_it(deploy):
    with pytest.raises(RuntimeError, match="only GitHub"):
        rollout._open_pr(
            _copy(), {"git_url": "https://gitlab.com/acme/rules.git"}, "", "ghp_secret"
        )
