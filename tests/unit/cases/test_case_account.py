"""A case's account merges its runs' accounts without losing whose claim is whose."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.cases import case_account
from core.cases.case_account import merge_accounts, read_case_account
from core.storage.schemas.case_account import CaseAccount, dump_account

FIXTURES = Path(__file__).resolve().parents[3] / "services/agent/tests/fixtures/account"

pytestmark = pytest.mark.unit


def fixture(name: str, run_id: str | None = None) -> CaseAccount:
    """A fixture account, renamed to another run when asked: the same loop run
    twice mints the same ids, which is what the merge must survive."""
    text = (FIXTURES / name).read_text()
    held = json.loads(text)
    if run_id is not None:
        text = text.replace(held["runs"][0]["run_id"], run_id)
    return CaseAccount.model_validate(json.loads(text))


def with_verdict(account: CaseAccount, disposition: str) -> CaseAccount:
    held = dump_account(account)
    held["verdict"]["disposition"] = disposition
    return CaseAccount.model_validate(held)


def keys(items) -> list[tuple[str, str]]:
    return [(item.run_id, item.id) for item in items]


def test_two_runs_of_one_kind_keep_every_item_by_run():
    first = fixture("investigate-01c1d889.json", "inv-1")
    second = fixture("investigate-01c1d889.json", "inv-2")
    merged = merge_accounts([first, second])

    # The same ids from two runs are two items, never one overwriting the other.
    assert keys(merged.evidence) == keys(first.evidence) + keys(second.evidence)
    assert len(set(keys(merged.evidence))) == len(merged.evidence)
    assert {item.id for item in merged.explanations} == {"h1", "h2"}
    assert len(merged.explanations) == 4
    # A bearing still names an explanation of its own run.
    for item in merged.evidence:
        own = {(one.run_id, one.id) for one in merged.explanations}
        assert all((item.run_id, bearing.on) in own for bearing in item.bears_on or [])
    assert [run.run_id for run in merged.runs] == ["inv-1", "inv-2"]


def test_the_latest_decisive_run_sets_the_verdict():
    decided = with_verdict(fixture("investigate-01c1d889.json", "inv-1"), "malicious")
    unsure = with_verdict(fixture("investigate-01c1d889.json", "inv-2"), "inconclusive")
    assert merge_accounts([decided, unsure]).verdict.run_id == "inv-1"
    later = with_verdict(fixture("investigate-01c1d889.json", "inv-3"), "benign")
    assert merge_accounts([decided, unsure, later]).verdict.run_id == "inv-3"


def test_with_no_decisive_run_the_latest_verdict_stands():
    first = with_verdict(fixture("investigate-01c1d889.json", "inv-1"), "needs_review")
    second = with_verdict(fixture("investigate-01c1d889.json", "inv-2"), "inconclusive")
    assert merge_accounts([first, second]).verdict.run_id == "inv-2"


def test_the_narrative_is_the_verdicts_runs():
    hunt = fixture("hunt-1e5c9131.json")
    investigation = fixture("investigate-01c1d889.json")
    # The hunt is later but inconclusive, so the investigation speaks for the case.
    merged = merge_accounts([investigation, hunt])
    assert merged.verdict.run_id == merged.narrative.run_id == investigation.runs[0].run_id


def test_explanations_group_by_role_and_keep_run_order():
    merged = merge_accounts([fixture("hunt-1e5c9131.json"), fixture("investigate-01c1d889.json")])
    roles = [item.role for item in merged.explanations]
    assert roles == sorted(roles, key=lambda role: case_account.ROLE_ORDER[role])
    adversaries = [item.run_id for item in merged.explanations if item.role == "adversary"]
    assert adversaries == ["1e5c9131-7490-41d1-8bc1-085758a8dcec", "01c1d889-5e0f-4686-860b-4813c0c41840"]


def test_the_timeline_is_in_time_order_across_runs():
    rca = fixture("root-cause-v2-sample.json")
    investigation = fixture("investigate-01c1d889.json")
    merged = merge_accounts([rca, investigation])
    timed = [entry.at for entry in merged.timeline if entry.at is not None]
    assert timed == sorted(timed)
    # An entry with no time stays beside the entries of its own run around it.
    untimed = [entry for entry in merged.timeline if entry.at is None]
    assert {entry.run_id for entry in untimed} == {rca.runs[0].run_id, investigation.runs[0].run_id}
    assert len(merged.timeline) == len(rca.timeline) + len(investigation.timeline)


def test_a_section_no_run_filled_stays_absent_and_an_empty_one_stays():
    hunt = fixture("hunt-1e5c9131.json")
    chain = fixture("root-cause-fec71eda.json")
    held = dump_account(merge_accounts([hunt, chain]))
    assert "accounts" not in held and "timeline" not in held
    assert held["escalations"] == []
    assert held["chain"]["run_id"] == chain.runs[0].run_id
    assert CaseAccount.model_validate(held) is not None


def test_a_case_with_no_runs_has_an_account_of_nothing():
    assert dump_account(merge_accounts([])) == {"schema_version": 1, "runs": []}


def test_every_fixture_merges_into_a_valid_account():
    accounts = [fixture(path.name) for path in sorted(FIXTURES.glob("*.json"))]
    held = dump_account(merge_accounts(accounts))
    assert dump_account(CaseAccount.model_validate(held)) == held
    assert len(held["runs"]) == len(accounts)


def test_reading_a_case_names_the_runs_it_could_not_read(monkeypatch):
    accounts = {"inv-1": fixture("investigate-01c1d889.json", "inv-1"), "chat-1": None}

    async def read(run_id: str):
        if run_id == "broken":
            raise RuntimeError("the agent layer answered 500")
        return accounts[run_id]

    monkeypatch.setattr(case_account, "read_account", read)
    result = asyncio.run(read_case_account(["inv-1", "broken", "chat-1"]))
    assert result.unread == ["broken"]
    assert [run.run_id for run in result.account.runs] == ["inv-1"]
