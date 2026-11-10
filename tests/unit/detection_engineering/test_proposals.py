"""The proposal's life on real Postgres (#4157), ported from 1.x's
``tests/unit/detection_engineering/test_proposals.py``. Events are invented and read
through ``siem_replay`` (the ``siem`` fixture stands in for a SIEM); the rule evaluator
is Vigil's. Shadow and live, and drafting with the model, are not in this change."""

import asyncio

import pytest

from core.detections.engineering import proposals, sigma_text
from core.detections.overlay import Refused
from core.storage.models.detection_proposal import InvalidProposalMove

from .conftest import RULE

SPLUNK_COPY = """
name: Office application starts encoded PowerShell
id: 5c1f0d3e-2b1a-4f7e-9a51-0d6c1b2f4e10
search: '`sysmon` EventCode=1 (ParentImage="*\\\\WINWORD.EXE" OR ParentImage="*\\\\EXCEL.EXE") Image="*\\\\powershell.exe" CommandLine="*-enc*"'
mitre_attack_id:
  - T1059.001
description: An Office application starts PowerShell with an encoded command.
"""


def _steps(view):
    return [
        (e["evidence"]["step"], e["to_state"], e["actor"], e["actor_kind"])
        for e in view["events"]
    ]


def _replay(pid, actor="dana.reviewer", **kw):
    return asyncio.run(proposals.replay(pid, actor, **kw))


def _tune(pid, exclusion, actor, reason):
    return asyncio.run(proposals.tune(pid, exclusion, actor, reason))


@pytest.fixture
def linted(add_case, siem, attack_row, plain_row):
    case_id = add_case()
    siem.add(attack_row, case_id=case_id)
    siem.add(plain_row)
    view = proposals.create(sigma=RULE, actor="dana.reviewer", case_id=case_id)
    return proposals.lint(view["proposal_id"], "dana.reviewer")


@pytest.fixture
def replayed(linted):
    return _replay(linted["proposal_id"])


def test_a_pasted_rule_is_a_named_draft_with_its_technique(db):
    view = proposals.create(sigma=RULE, actor="dana.reviewer")
    assert view["state"] == "draft" and view["technique"] == "T1059.001"
    assert view["title"] == "Office application starts encoded PowerShell"
    assert _steps(view) == [("draft", "draft", "dana.reviewer", "person")]
    assert view["events"][0]["evidence"]["filled_in"] == ["added id"]


def test_an_agents_draft_says_so(db):
    view = proposals.create(sigma=RULE, actor="agent-run-7", kind="agent")
    assert view["origin"]["source"] == "saved by an agent"
    assert _steps(view) == [("draft", "draft", "agent-run-7", "agent")]
    with pytest.raises(Refused, match="not 'token'"):
        proposals.lint(view["proposal_id"], "x", kind="token")


def test_a_failed_lint_stays_on_the_record_and_moves_nothing(db):
    bad = RULE.replace("CommandLine|contains: '-enc'", "DestinationIp: 203.0.113.9")
    view = proposals.lint(
        proposals.create(sigma=bad, actor="dana.reviewer")["proposal_id"], "lee.analyst"
    )
    assert view["state"] == "draft"
    assert view["latest"]["lint"]["evidence"]["passed"] is False
    assert view["latest"]["lint"]["actor"] == "lee.analyst"
    assert view["allowed"] == ["linted", "rejected"]


def test_replay_counts_hits_and_false_positives_on_events_no_case_claims(
    add_case, siem, attack_row, signed_macro_row, plain_row
):
    case_id = add_case()
    siem.add(attack_row, case_id=case_id)
    siem.add(plain_row, case_id=case_id)
    false_positive = siem.add(signed_macro_row)
    siem.add(plain_row)
    siem.add(signed_macro_row, age_days=60)  # outside the window
    other_case = add_case("case-de-2")
    siem.add(signed_macro_row, case_id=other_case)  # another Case's: claimed

    view = proposals.create(sigma=RULE, actor="dana.reviewer", case_id=case_id)
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"])
    evidence = view["latest"]["replay"]["evidence"]

    assert view["state"] == "replayed"
    assert (evidence["hits"], evidence["attack_rows"]) == (1, 2)
    assert (evidence["false_positives"], evidence["benign_rows"]) == (1, 2)
    (elastic,) = evidence["siems"]
    assert elastic["false_positive_samples"][0]["id"] == false_positive
    assert (
        elastic["claimed_left_out"] == 1
    )  # case-de-2's; the Case's own are attack rows
    assert evidence["proven"] is False
    assert "no Case claims" in evidence["unproven_because"]


def test_replay_agrees_with_check_detection_candidate(linted, attack_row, plain_row):
    from core.detections.tools import get_security_detection_tools

    view = _replay(linted["proposal_id"])
    checked = asyncio.run(
        get_security_detection_tools().check_detection_candidate(
            rule_yaml=view["sigma"],
            events=[attack_row],
            ruled_out_events=[plain_row],
            technique_id="T1059.001",
            hostname="host-case-de-1",
            started_at="2026-11-01T00:00:00Z",
            ended_at="2026-11-02T00:00:00Z",
        )
    )
    assert checked["candidate"] is not None
    assert view["latest"]["replay"]["evidence"]["proven"] is True


def test_a_tune_reruns_lint_and_replay_and_leaves_the_before_on_the_record(
    add_case, siem, attack_row, signed_macro_row, plain_row
):
    case_id = add_case()
    siem.add(attack_row, case_id=case_id)
    siem.add(signed_macro_row)
    siem.add(plain_row)
    view = proposals.create(sigma=RULE, actor="dana.reviewer", case_id=case_id)
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"])
    assert view["latest"]["replay"]["evidence"]["false_positives"] == 1
    old_sigma = view["sigma"]

    view = _tune(
        view["proposal_id"],
        sigma_text.Exclusion("ParentCommandLine", "contains", ("trusted-macros",)),
        "lee.analyst",
        "Signed macros from the finance share",
    )

    assert view["state"] == "replayed"
    assert view["sigma"] != old_sigma
    after = view["latest"]["replay"]["evidence"]
    assert (after["hits"], after["false_positives"], after["proven"]) == (1, 0, True)
    tune = view["latest"]["tune"]
    assert (
        tune["actor"] == "lee.analyst"
        and tune["evidence"]["before"]["false_positives"] == 1
    )
    assert tune["reason"] == "Signed macros from the finance share"
    old_events = [e for e in view["events"] if e["sigma"] == old_sigma]
    assert [e["evidence"]["step"] for e in old_events] == ["draft", "lint", "replay"]


def test_a_tune_needs_a_reason(replayed):
    with pytest.raises(Refused, match="why"):
        _tune(
            replayed["proposal_id"],
            sigma_text.Exclusion("Image", "contains", ("x",)),
            "lee.analyst",
            "  ",
        )


def test_a_replay_that_read_no_siem_is_not_evaluated_and_says_what_is_needed(db, siem):
    view = proposals.create(sigma=RULE, actor="dana.reviewer")
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"])
    assert view["state"] == "linted"
    evidence = view["latest"]["replay"]["evidence"]
    assert evidence["evaluated"] is False
    assert "No SIEM was read" in evidence["unproven_because"]


def test_a_rule_vigil_cannot_evaluate_stays_linted_with_the_reason(siem, db, plain_row):
    siem.add(plain_row)
    timed = RULE.replace(
        "condition: selection_parent and selection_child",
        "condition: selection_parent | count() > 3",
    )
    view = proposals.create(sigma=timed, actor="dana.reviewer")
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"])
    assert view["state"] == "linted"
    evidence = view["latest"]["replay"]["evidence"]
    assert evidence["evaluated"] is False and evidence["unproven_because"]


def test_zero_false_positives_with_no_event_holding_the_fields_proves_nothing(
    add_case, siem, attack_row
):
    case_id = add_case()
    siem.add(attack_row, case_id=case_id)
    siem.add({"QueryName": "example.test"})
    view = proposals.create(sigma=RULE, actor="dana.reviewer", case_id=case_id)
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"])
    evidence = view["latest"]["replay"]["evidence"]
    assert (evidence["hits"], evidence["false_positives"]) == (1, 0)
    assert evidence["benign_holding_fields"] == 0
    assert evidence["proven"] is False
    assert "holds a field the rule reads" in evidence["unproven_because"]


def test_an_unproven_rule_converts_only_with_a_persons_written_reason(
    siem, db, plain_row, attack_row
):
    siem.add(plain_row)
    siem.add(attack_row)
    view = proposals.create(sigma=RULE, actor="dana.reviewer")
    view = proposals.lint(view["proposal_id"], "dana.reviewer")
    view = _replay(view["proposal_id"], named={"elastic": ["no-such-event"]})
    assert view["latest"]["replay"]["evidence"]["attack_rows"] == 0
    with pytest.raises(Refused, match="did not prove"):
        proposals.convert(
            view["proposal_id"], "dana.reviewer", pasted={"splunk": SPLUNK_COPY}
        )
    view = proposals.convert(
        view["proposal_id"],
        "dana.reviewer",
        pasted={"splunk": SPLUNK_COPY},
        accept_reason="No telemetry from this host class yet",
    )
    assert view["state"] == "converted"
    accepted = view["latest"]["convert"]["evidence"]["accepted_unproven"]
    assert accepted["by"] == "dana.reviewer"
    assert accepted["reason"] == "No telemetry from this host class yet"


def test_a_pasted_copy_is_labelled_pasted_with_who_pasted_it(replayed):
    view = proposals.convert(
        replayed["proposal_id"], "lee.analyst", pasted={"splunk": SPLUNK_COPY}
    )
    rows = {r["target"]: r for r in view["latest"]["convert"]["evidence"]["targets"]}
    assert view["state"] == "converted"
    assert (
        rows["splunk"]["status"] == "pasted"
        and rows["splunk"]["pasted_by"] == "lee.analyst"
    )
    assert "search:" in view["copies"]["splunk"]
    assert view["latest"]["convert"]["evidence"]["siem_written"] is False


def test_a_copy_the_overlay_would_refuse_is_reported_and_moves_nothing(replayed):
    keyed = SPLUNK_COPY.replace('CommandLine="*-enc*"', 'host="WS-FIN-07"')
    view = proposals.convert(
        replayed["proposal_id"], "lee.analyst", pasted={"splunk": keyed}
    )
    (row,) = view["latest"]["convert"]["evidence"]["targets"]
    assert row["status"] == "refused" and "lint" in row["reason"]
    assert view["state"] == "replayed" and view["copies"] == {}


@pytest.mark.parametrize(
    "move",
    [
        lambda pid: asyncio.run(proposals.replay(pid, "x")),
        lambda pid: proposals.convert(pid, "x", pasted={"splunk": SPLUNK_COPY}),
    ],
    ids=["replay", "convert"],
)
def test_a_step_cannot_be_skipped_from_draft(db, move):
    pid = proposals.create(sigma=RULE, actor="x")["proposal_id"]
    with pytest.raises(InvalidProposalMove):
        move(pid)
    assert proposals.get(pid)["state"] == "draft"


def test_a_rejected_proposal_keeps_its_history_and_cannot_move(linted):
    pid = linted["proposal_id"]
    view = proposals.reject(pid, "dana.reviewer", "Duplicate of another proposal")
    assert view["state"] == "rejected" and view["allowed"] == []
    assert view["latest"]["reject"]["reason"] == "Duplicate of another proposal"
    with pytest.raises(InvalidProposalMove):
        proposals.lint(pid, "dana.reviewer")
    with pytest.raises(InvalidProposalMove):
        proposals.edit(pid, RULE, "dana.reviewer")


def test_editing_goes_back_to_draft_and_the_old_replay_no_longer_counts(replayed):
    pid = replayed["proposal_id"]
    proposals.convert(pid, "lee.analyst", pasted={"splunk": SPLUNK_COPY})
    view = proposals.edit(pid, replayed["sigma"].replace("high", "low"), "lee.analyst")
    assert view["state"] == "draft" and view["copies"] == {}
    assert view["latest"]["replay"]["judges_current_text"] is False
    with pytest.raises(InvalidProposalMove):
        proposals.convert(pid, "lee.analyst", pasted={"splunk": SPLUNK_COPY})


def test_a_replay_of_text_edited_meanwhile_is_not_kept(linted, monkeypatch):
    pid = linted["proposal_id"]
    real = proposals.siem_replay.run

    async def edited_while_reading(sigma, **kw):
        out = await real(sigma, **kw)
        proposals.edit(pid, sigma.replace("high", "low"), "lee.analyst")
        return out

    monkeypatch.setattr(proposals.siem_replay, "run", edited_while_reading)
    with pytest.raises(Refused, match="changed while it was being replayed"):
        _replay(pid)
    view = proposals.get(pid)
    assert view["state"] == "draft" and view["latest"]["replay"] is None
