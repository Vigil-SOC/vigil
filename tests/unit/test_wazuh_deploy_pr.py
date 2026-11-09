"""A Wazuh overlay rule is deployed to the customer's Wazuh rule repo as a pull
request: one file per rule, vigil_<id>.xml inside a <group>, through the same
PR flow as Splunk and Elastic (branch, supersede, in-repo, host guard). Its id
stays custom and unclaimed, it never sets overwrite, and Vigil never writes to
the manager. Until Vigil can run Wazuh rules in shadow, a person may deploy one
whatever its stage, and the PR and its trail row say it was not shadow-tested."""

from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from test_deploy_supersedes_old_pr import REPO, TOKEN, FakeGitHub

import core.secrets
from core.detections import detection_rules_service as service
from core.detections.inventory import rollout, sync

TARGET = {"git_url": f"https://github.com/{REPO}.git", "subdirectory": "etc/rules"}
BARE = (
    '<rule id="100100" level="12">\n'
    "  <if_sid>61603</if_sid>\n"
    "  <description>Rundll32 proxying JavaScript</description>\n"
    "</rule>\n"
)
GROUPED = f'<group name="windows,">\n{BARE}</group>\n'


def _copy(version=1, text=BARE, stage="", key="100100", deploy=None):
    return rollout.Copy(
        rule_id=16735,
        version=version,
        name="Rundll32 proxying JavaScript",
        language="wazuh",
        stage=stage,
        stage_at=rollout.NO_STAGE_AT,
        checks={"deploy": deploy} if deploy else {},
        rule={},
        key=key,
        toml=text,
        path=f"wazuh/{key}.xml",
    )


class _Session:
    """The manager's loaded rules with this id: their file paths."""

    def __init__(self, paths):
        self.paths = paths

    def scalars(self, _query):
        return list(self.paths)


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw),
    )
    return fake


@pytest.fixture
def deploy(github, monkeypatch):
    """_deploy with the DB stubbed; ``loaded`` are the manager's paths for the id."""
    box = {"loaded": [], "recorded": {}}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield _Session(box["loaded"])

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: TOKEN)

    def record(c, out, *_):
        box["recorded"][(c.rule_id, c.version)] = out
        return out

    monkeypatch.setattr(rollout, "_record_deploy", record)
    monkeypatch.setattr(rollout, "_record_superseded", lambda *a: None)

    def run(copy, target=TARGET):
        rollout._deploy(
            [copy],
            {"wazuh": target},
            datetime.now(timezone.utc),
            rollout.RolloutReport(),
        )
        return box["recorded"][(copy.rule_id, copy.version)]

    run.box = box
    return run


def _pr(github):
    return github.pulls[-1]


def test_a_wazuh_rule_goes_as_its_own_file_wrapped_in_a_group(github, deploy):
    out = deploy(_copy())
    assert out["pr_url"] == f"https://github.com/{REPO}/pull/1"
    data = github.files[("vigil/rule-16735-v1", "etc/rules/vigil_100100.xml")]
    assert data.decode() == f'<group name="vigil,">\n{BARE.strip()}\n</group>\n'
    assert (
        _pr(github)["title"] == "Add Vigil rule Rundll32 proxying JavaScript (100100)"
    )


def test_a_grouped_wazuh_rule_goes_exactly_as_stored(github, deploy):
    deploy(_copy(text=GROUPED))
    assert github.files[("vigil/rule-16735-v1", "etc/rules/vigil_100100.xml")] == (
        GROUPED.encode()
    )


def test_a_person_deploying_an_untested_rule_is_told_in_the_pr_and_the_record(
    github, deploy, monkeypatch
):
    bodies = []
    real = rollout._pr_body
    monkeypatch.setattr(
        rollout,
        "_pr_body",
        lambda c, e, again=None: bodies.append(real(c, e, again)) or bodies[-1],
    )
    out = deploy(_copy(stage=""))
    assert out["note"] == rollout.UNTESTED
    assert rollout.UNTESTED in bodies[0]
    assert "One file per rule" in bodies[0] and "does not set overwrite" in bodies[0]


def test_a_live_wazuh_rule_says_it_is_live_and_carries_no_note(
    github, deploy, monkeypatch
):
    bodies = []
    real = rollout._pr_body
    monkeypatch.setattr(
        rollout,
        "_pr_body",
        lambda c, e, again=None: bodies.append(real(c, e, again)) or bodies[-1],
    )
    out = deploy(_copy(stage="live"))
    assert "note" not in out
    assert "is live in Vigil" in bodies[0] and rollout.UNTESTED not in bodies[0]


def test_overwrite_is_refused_before_any_call(github, deploy):
    out = deploy(_copy(text=BARE.replace('level="12"', 'level="12" overwrite="yes"')))
    assert "overwrite" in out["error"] and "pr_url" not in out
    assert github.calls == []


@pytest.mark.parametrize("key", ["5710", "120001"])
def test_an_id_outside_the_custom_range_is_refused(github, deploy, key):
    out = deploy(_copy(text=BARE.replace("100100", key), key=key))
    assert "outside Wazuh's range" in out["error"] and github.calls == []


def test_an_id_another_manager_rule_now_holds_is_refused(github, deploy):
    deploy.box["loaded"] = ["etc/rules/local_rules.xml"]
    out = deploy(_copy())
    assert "now loads another rule as id 100100" in out["error"]
    assert github.calls == []


def test_the_manager_loading_vigils_own_file_is_no_refusal(github, deploy):
    # A merged earlier version is loaded from vigil_100100.xml: the new one may follow.
    deploy.box["loaded"] = ["etc/rules/vigil_100100.xml"]
    assert "pr_url" in deploy(_copy(version=2))


def test_the_existing_flow_supersedes_and_finds_a_file_already_there(github, deploy):
    v1 = github.pr("vigil/rule-16735-v1")
    deploy(_copy(version=2))
    assert github.pulls[v1 - 1]["state"] == "closed"
    github.files[("main", "etc/rules/vigil_100100.xml")] = (
        f'<group name="vigil,">\n{BARE.strip()}\n</group>\n'.encode()
    )
    out = deploy(_copy(version=3))
    assert out["in_repo"] == "etc/rules/vigil_100100.xml on main"


def test_a_non_github_host_gets_no_request(github, deploy):
    out = deploy(_copy(), target={"git_url": "https://gitlab.com/acme/wazuh.git"})
    assert "pr_url" not in out and github.calls == []


def test_a_wazuh_repo_can_be_the_deploy_target():
    assert "wazuh" in service.DEPLOY_FORMATS
    svc = service.DetectionRulesService.__new__(service.DetectionRulesService)
    svc.sources = []
    svc._check_deploy_target(
        {"type": "git", "format": "wazuh", "git_url": "https://github.com/acme/wazuh"}
    )


def test_overlay_wazuh_copies_are_linted_on_sync():
    assert "wazuh" in sync.LINTED_LANGUAGES
    assert "wazuh" in sync.STAGED_LANGUAGES  # Vigil runs it in shadow (#4061)


# --- the person's deploy button ------------------------------------------------


@pytest.fixture
def overlay(monkeypatch):
    copies = [
        _copy(stage="", deploy=None),
        rollout.Copy(
            rule_id=16720,
            version=1,
            name="splunk shadow",
            language="splunk",
            stage="shadow",
            stage_at=datetime.now(timezone.utc),
            checks={},
            rule={},
            key="k",
            toml="x",
        ),
    ]
    copies[0].checks["lint"] = {"result": "pass"}
    monkeypatch.setattr(rollout, "_copies", lambda: copies)
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"wazuh": TARGET})
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)  # on Act
    return copies


def test_the_overlay_only_list_offers_a_wazuh_copy_and_says_it_is_untested(overlay):
    rows = {r["id"]: r for r in rollout.overlay_only()}
    assert set(rows) == {16735}  # a shadow Splunk copy is not offered
    assert rows[16735]["untested"] == rollout.UNTESTED
    assert rows[16735]["deploy_target"] is True


def test_a_person_can_deploy_a_wazuh_copy_whatever_its_stage(overlay, monkeypatch):
    sent = []
    monkeypatch.setattr(
        rollout,
        "_deploy",
        lambda todo, targets, now, report, actor, again=None: sent.append((todo, actor))
        or {c.rule_id: {"pr_url": "u", "note": rollout.UNTESTED} for c in todo},
    )
    out = rollout.deploy_overlay_only([16735, 16720], actor="ana")
    assert [c.rule_id for c in sent[0][0]] == [16735] and sent[0][1] == "ana"
    assert out["opened"] == [{"rule_id": 16735, "pr_url": "u"}]
    assert out["skipped"] == [
        {
            "rule_id": 16720,
            "reason": "not a live rule only Vigil runs, nor a Wazuh copy",
        }
    ]


def test_a_wazuh_copy_that_has_not_passed_lint_is_not_deployed(overlay, monkeypatch):
    overlay[0].checks["lint"] = {"result": "fail"}
    monkeypatch.setattr(rollout, "_deploy", lambda todo, *a, **k: {})
    out = rollout.deploy_overlay_only([16735], actor="ana")
    assert out["skipped"] == [
        {"rule_id": 16735, "reason": "its current version has not passed lint"}
    ]


def test_a_rule_whose_only_group_is_its_own_child_is_still_wrapped(github, deploy):
    own = BARE.replace("  <if_sid>", "  <group>sysmon,</group>\n  <if_sid>")
    deploy(_copy(text="<!-- not a <group> -->\n" + own))
    data = github.files[("vigil/rule-16735-v1", "etc/rules/vigil_100100.xml")]
    assert data.decode().startswith('<group name="vigil,">\n<!-- not a <group> -->')


def test_an_xml_declaration_is_dropped_when_the_rule_is_wrapped(github, deploy):
    deploy(_copy(text='<?xml version="1.0"?>\n' + BARE))
    data = github.files[("vigil/rule-16735-v1", "etc/rules/vigil_100100.xml")]
    assert data.decode() == f'<group name="vigil,">\n{BARE.strip()}\n</group>\n'


def test_a_key_with_a_leading_zero_is_refused(github, deploy):
    out = deploy(_copy(text=BARE.replace("100100", "0100100"), key="0100100"))
    assert "outside Wazuh's range" in out["error"] and github.calls == []


def test_the_untested_note_says_this_copy_was_not_validated():
    # Vigil can validate a Wazuh copy in shadow now (#4061); the note is about
    # this copy, not a missing capability.
    assert "can't validate Wazuh rules" not in rollout.UNTESTED
    assert "not shadow-tested" in rollout.UNTESTED


def test_a_customer_file_whose_name_only_ends_like_vigils_is_refused(github, deploy):
    deploy.box["loaded"] = ["etc/rules/xvigil_100100.xml"]
    out = deploy(_copy())
    assert "now loads another rule as id 100100" in out["error"]
    assert github.calls == []


def test_a_key_not_written_exactly_as_the_id_is_refused(github, deploy):
    out = deploy(_copy(key=" 100100"))
    assert "outside Wazuh's range" in out["error"] and github.calls == []


def test_a_person_retries_a_wazuh_deploy_whose_supersede_failed(overlay, monkeypatch):
    overlay[0].checks["deploy"] = {
        "version": 1,
        "pr_url": "https://github.com/acme/wazuh/pull/2",
        "supersede_error": "closing PR #1 failed: 502",
    }
    sent = []
    monkeypatch.setattr(
        rollout,
        "_deploy",
        lambda todo, *a, **k: sent.extend(todo)
        or {c.rule_id: {"pr_url": "u"} for c in todo},
    )
    out = rollout.deploy_overlay_only([16735], actor="ana")
    assert [c.rule_id for c in sent] == [16735]
    assert out["opened"] == [{"rule_id": 16735, "pr_url": "u"}]
