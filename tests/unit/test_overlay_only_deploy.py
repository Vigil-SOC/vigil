"""Add to SIEM from the Stop prompt: the live rules only Vigil runs get their
deploy PR opened now, by the same direct GitHub calls the rollout sweep makes,
and each row reads what its PR came to from checks['deploy']."""

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import core.secrets
from core.detections.detection_rules_service import GITHUB_ONLY
from core.detections.inventory import rollout

TARGET = {"git_url": "https://github.com/acme/detections.git", "subdirectory": "rules"}


def _copy(
    rule_id,
    language="elastic",
    stage="live",
    toml="[rule]\n",
    deploy=None,
    lint=None,
):
    # A lint pass unless the test says otherwise; {} is a version with no lint result.
    lint = {"result": "pass"} if lint is None else lint
    checks = {"lint": lint} if lint else {}
    if deploy:
        checks["deploy"] = deploy
    return rollout.Copy(
        rule_id=rule_id,
        version=2,
        name=f"rule {rule_id}",
        language=language,
        stage=stage,
        stage_at=datetime.now(timezone.utc),
        checks=checks,
        rule={},
        key=f"key-{rule_id}",
        toml=toml,
        path=f"{rule_id}.toml",
    )


@pytest.fixture
def estate(monkeypatch):
    state = {
        "copies": [
            _copy(1),  # nothing yet
            _copy(
                2, deploy={"version": 2, "pr_url": "https://github.com/acme/d/pull/7"}
            ),
            _copy(3, language="splunk"),  # no splunk target
            _copy(4, stage="shadow"),
            _copy(5),  # handed off to a SIEM copy
            _copy(6, deploy={"version": 2, "error": "POST /pulls: 422"}),  # retried
            _copy(7, deploy={"version": 1, "pr_url": "https://old/pull/1"}),  # stale
            _copy(8, deploy={"version": 2, "in_repo": "rules/8.toml on main"}),
        ],
        "gone": {5},
        "targets": {"elastic": dict(TARGET)},
        "stopped": False,
        "fail": set(),
        "opened": [],
        "recorded": {},
    }

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    def open_pr(copy, target, body, token, reopen=False, again=False):
        state["opened"].append((copy.rule_id, target["git_url"], token))
        if copy.rule_id in state["fail"]:
            raise RuntimeError("POST /repos/acme/detections/pulls: 502 bad gateway")
        return {"pr_url": f"https://github.com/acme/detections/pull/{copy.rule_id}"}

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_copies", lambda: state["copies"])
    monkeypatch.setattr(rollout, "handed_off", lambda ids: state["gone"] & set(ids))
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: state["targets"])
    monkeypatch.setattr(rollout, "_stopped", lambda: state["stopped"])
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)  # Act
    monkeypatch.setattr(rollout, "_open_pr", open_pr)

    def record(copy, out, actor="system"):
        state["recorded"][copy.rule_id] = out
        return out

    monkeypatch.setattr(rollout, "_record_deploy", record)
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: "ghp_x")
    # A recorded PR's state is GitHub's to say (#4123): here every one is open.
    monkeypatch.setattr(rollout, "_pr_state", lambda url, target, token: "open")
    monkeypatch.setattr(rollout, "_record_pr_state", lambda *a: None)
    return state


def test_each_row_reads_its_deploy_state_from_its_current_version(estate):
    rows = {r["id"]: r for r in rollout.overlay_only()}
    assert sorted(rows) == [1, 2, 3, 6, 7, 8]  # not shadow (4) nor handed off (5)
    assert rows[1]["deploy_target"] and rows[1]["deploy"] is None
    assert rows[2]["deploy"]["pr_url"].endswith("/pull/7")
    assert not rows[3]["deploy_target"]
    assert rows[6]["deploy"]["error"]
    assert rows[7]["deploy"] is None  # an earlier version's PR isn't this one's
    assert rows[8]["deploy"]["in_repo"]


def test_add_all_opens_what_can_go_and_retries_a_failed_one(estate):
    out = rollout.deploy_overlay_only()
    assert [o["rule_id"] for o in out["opened"]] == [1, 6, 7]
    assert out["opened"][0]["pr_url"].endswith("/pull/1")
    assert out["failed"] == []
    assert {s["rule_id"]: s["reason"] for s in out["skipped"]} == {
        2: "its pull request is already open: https://github.com/acme/d/pull/7",
        3: "no splunk deploy target is set",
        8: "it is already in the repo: rules/8.toml on main",
    }
    # Recorded on each version, as the sweep records it.
    assert estate["recorded"][1]["pr_url"].endswith("/pull/1")
    assert estate["recorded"][1]["version"] == 2


def test_a_pr_that_fails_says_why(estate):
    estate["fail"] = {1}
    out = rollout.deploy_overlay_only([1])
    assert out["opened"] == []
    assert out["failed"] == [
        {
            "rule_id": 1,
            "error": "POST /repos/acme/detections/pulls: 502 bad gateway",
            "reason": "GitHub error. Try again.",
        }
    ]
    assert "502" in estate["recorded"][1]["error"]


def test_named_rules_say_why_others_are_skipped(estate):
    out = rollout.deploy_overlay_only([1, 4, 5, 99])
    assert [o["rule_id"] for o in out["opened"]] == [1]
    assert {s["rule_id"] for s in out["skipped"]} == {4, 5, 99}


@pytest.mark.parametrize(
    "url",
    ["https://gitlab.com/acme/detections.git", "git@bitbucket.org:acme/d.git"],
)
def test_a_non_github_target_gets_no_request(estate, url):
    estate["targets"]["elastic"]["git_url"] = url
    out = rollout.deploy_overlay_only([1])
    assert estate["opened"] == []
    assert out["skipped"] == [{"rule_id": 1, "reason": GITHUB_ONLY}]


def test_a_marked_github_enterprise_target_is_deployed_to(estate):
    estate["targets"]["elastic"] = {
        "git_url": "https://ghe.acme.internal/soc/detections.git",
        "github_enterprise": True,
    }
    rollout.deploy_overlay_only([1])
    assert estate["opened"] == [
        (1, "https://ghe.acme.internal/soc/detections.git", "ghp_x")
    ]


def test_nothing_opens_while_stopped(estate):
    estate["stopped"] = True
    out = rollout.deploy_overlay_only()
    assert estate["opened"] == []
    assert {s["rule_id"]: s["reason"] for s in out["skipped"]}[1] == (
        "Vigil is stopped; it opens no pull request"
    )


def test_a_splunk_rule_needs_its_overlay_file(estate, monkeypatch):
    estate["targets"] = {"splunk": dict(TARGET)}
    estate["copies"][2] = _copy(3, language="splunk", toml="")
    monkeypatch.setattr(rollout, "_read_splunk_files", lambda copies: None)
    out = rollout.deploy_overlay_only([3])
    assert estate["opened"] == []
    assert out["skipped"] == [
        {"rule_id": 3, "reason": "its overlay file could not be read"}
    ]


# --- the Stop prompt's rows say what a person can do ---------------------------


def test_a_refused_target_is_no_target_for_the_button(estate):
    estate["targets"]["elastic"]["git_url"] = "https://gitlab.com/acme/detections.git"
    row = {r["id"]: r for r in rollout.overlay_only()}[1]
    assert row["deploy_target"] is False
    assert row["target_refused"] == GITHUB_ONLY


def test_a_github_target_is_not_refused(estate):
    row = {r["id"]: r for r in rollout.overlay_only()}[1]
    assert row["deploy_target"] is True and row["target_refused"] is None


def test_a_failed_row_says_what_to_do_and_keeps_the_raw_line(estate):
    row = {r["id"]: r for r in rollout.overlay_only()}[6]
    assert row["deploy"]["error"] == "POST /pulls: 422"
    assert row["deploy"]["reason"].startswith("GitHub refused the PR")


@pytest.mark.parametrize(
    "raw, starts",
    [
        ("no GitHub token: set the github integration's token", "No GitHub token"),
        ("https://gitlab.com/x.git: " + GITHUB_ONLY, "Not a GitHub repo"),
        ("GET /repos/a/b: 401 Bad credentials", "Token not accepted"),
        (
            "POST /repos/a/b/pulls: 403 Resource not accessible by personal access token",
            "Token can't write here",
        ),
        ("GET /repos/a/b: 404 Not Found", "Repo not found"),
        ("PUT /api/v3/repos/a/b/contents/x: 500 boom", "GitHub error"),
        ("ConnectTimeout", "GitHub didn't answer"),
        ("something else entirely", "Couldn't open the PR"),
    ],
)
def test_plain_reasons(raw, starts):
    assert rollout.plain_deploy_error(raw).startswith(starts)


# --- a raced click resolves to the PR the other one opened -------------------


class _RacedGitHub:
    """A repo where another click made the branch, wrote the file or opened the
    PR between this call's checks and its writes."""

    def __init__(self, opened_by_other_after):
        self.calls = 0
        self.lists = 0
        self.opened_after = opened_by_other_after  # PR lists before the other PR shows

    def handle(self, request):
        import httpx

        self.calls += 1
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/pulls"):
            self.lists += 1
            if self.lists > self.opened_after:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "html_url": "https://github.com/acme/d/pull/9",
                            "state": "open",
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/repos/acme/detections":
            return httpx.Response(200, json={"default_branch": "main"})
        if method == "GET" and "/contents/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "GET" and "/git/ref/heads/" in path:
            if path.endswith("/main"):
                return httpx.Response(200, json={"object": {"sha": "s"}})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(422, json={"message": "Reference already exists"})
        if method == "PUT":
            return httpx.Response(409, json={"message": "sha wasn't supplied"})
        if method == "POST" and path.endswith("/pulls"):
            return httpx.Response(
                422, json={"message": "A pull request already exists"}
            )
        return httpx.Response(404, json={"message": "no route"})


@pytest.fixture
def raced(monkeypatch):
    import httpx

    def make(opened_after):
        fake = _RacedGitHub(opened_after)
        real = httpx.Client
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw),
        )
        monkeypatch.setattr(rollout, "RACE_WAITS", (0.0, 0.0, 0.0))
        return fake

    return make


@pytest.mark.parametrize("opened_after", [1, 2, 3])
def test_a_raced_click_resolves_to_the_other_ones_pr(raced, opened_after):
    # The first look finds nothing; the other click's PR shows up a little later.
    raced(opened_after)
    out = rollout._open_pr(_copy(1), TARGET, "body", "ghp_x")
    assert out == {"pr_url": "https://github.com/acme/d/pull/9"}


def test_a_real_failure_still_fails_after_looking_again(raced):
    raced(99)  # no other PR ever shows up
    with pytest.raises(RuntimeError, match="409"):
        rollout._open_pr(_copy(1), TARGET, "body", "ghp_x")


@pytest.mark.parametrize(
    "lint, reason",
    [
        ({}, "its current version has not passed lint"),
        (
            {"result": "fail", "findings": [{"kind": "host", "value": "dc01"}]},
            "its current version has not passed lint",
        ),
    ],
)
def test_add_to_siem_skips_a_version_without_a_lint_pass(estate, lint, reason):
    estate["copies"] = [_copy(1, lint=lint), _copy(9)]
    out = rollout.deploy_overlay_only([1, 9])
    assert [o["rule_id"] for o in out["opened"]] == [9]
    assert {s["rule_id"]: s["reason"] for s in out["skipped"]} == {1: reason}
    assert [o[0] for o in estate["opened"]] == [9]
