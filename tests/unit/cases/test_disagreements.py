"""Where a case's runs part ways, found by lining their accounts up."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.cases.case_account import merge_accounts
from core.cases.disagreements import find_disagreements
from core.storage.schemas.case_account import CaseAccount, dump_account

FIXTURES = Path(__file__).resolve().parents[3] / "services/agent/tests/fixtures/account"

pytestmark = pytest.mark.unit


def fixture(name: str) -> CaseAccount:
    return CaseAccount.model_validate(json.loads((FIXTURES / name).read_text()))


def account(run_id: str, kind: str = "investigate", **sections) -> CaseAccount:
    return CaseAccount.model_validate(
        {
            "schema_version": 1,
            "runs": [{"run_id": run_id, "run_kind": kind}],
            **sections,
        }
    )


def verdict(run_id: str, disposition: str) -> dict:
    return {"run_id": run_id, "disposition": disposition, "headline": "h"}


def claim(run_id, id, text, standing, role="adversary", techniques=None):
    held = {
        "run_id": run_id,
        "id": id,
        "claim": text,
        "role": role,
        "standing": standing,
    }
    return {**held, "techniques": techniques} if techniques else held


def test_the_nightshift_runs_disagree_on_exfiltration():
    # The hunt saw no outbound transfer; the investigation holds the 4.8 GB one.
    found = find_disagreements(
        [
            fixture("hunt-1e5c9131.json"),
            fixture("hunt-208b5901.json"),
            fixture("investigate-01c1d889.json"),
            fixture("root-cause-v2-nightshift.json"),
        ]
    )

    assert [one.kind for one in found] == ["gap_answered"]
    assert found[0].text.startswith(
        'hunt 1e5c9131 could not see one thing that investigate 01c1d889 holds records on: "No large outbound'
    )
    # The quote closes the line; a period after it would end on '".'.
    assert found[0].text.endswith('"')
    assert ("01c1d889-5e0f-4686-860b-4813c0c41840", "r19.0") in [
        (ref.run_id, ref.id) for ref in found[0].refs or []
    ]


def test_opposite_verdicts_are_one_disagreement_naming_every_run():
    found = find_disagreements(
        [
            account("aaaaaaaa-1", verdict=verdict("aaaaaaaa-1", "malicious")),
            account("bbbbbbbb-2", verdict=verdict("bbbbbbbb-2", "benign")),
            account("cccccccc-3", verdict=verdict("cccccccc-3", "false_positive")),
            account("dddddddd-4", verdict=verdict("dddddddd-4", "inconclusive")),
        ]
    )

    assert [(one.kind, one.runs) for one in found] == [
        ("verdict", ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"])
    ]
    assert "investigate aaaaaaaa says malicious" in found[0].text


def test_a_run_holding_out_for_review_objects_to_a_dismissal():
    found = find_disagreements(
        [
            account("a", verdict=verdict("a", "needs_review")),
            account("b", verdict=verdict("b", "benign")),
        ]
    )

    assert [(one.kind, one.runs) for one in found] == [("verdict", ["a", "b"])]


def test_a_run_that_could_not_decide_disagrees_with_nobody():
    found = find_disagreements(
        [
            account("a", verdict=verdict("a", "malicious")),
            account("b", verdict=verdict("b", "inconclusive")),
        ]
    )

    assert found == []


def test_a_claim_held_by_one_run_and_ruled_out_by_another_is_paired_by_what_it_names():
    held = claim(
        "a", "h1", "lsass dumped on FIN-WKS-014", "proven", techniques=["T1003.001"]
    )
    dropped = claim(
        "b",
        "h1",
        "Credential theft on FIN-WKS-014",
        "ruled_out",
        techniques=["T1003.001"],
    )
    weakened = claim("c", "h4", "comsvcs MiniDump (T1003.001)", "weakened")
    elsewhere = claim("b", "h2", "Beaconing from WEB-02", "ruled_out")
    benign = claim("b", "h3", "Backup job on FIN-WKS-014", "ruled_out", role="benign")
    unrelated = claim("c", "h5", "Beaconing from WEB-03", "ruled_out")

    found = find_disagreements(
        [
            account("a", explanations=[held]),
            account("b", explanations=[dropped, elsewhere, benign]),
            account("c", explanations=[weakened, unrelated]),
        ]
    )

    # One disagreement for the held claim, naming every run that dropped it.
    assert [
        (one.kind, one.runs, [(r.run_id, r.id) for r in one.refs or []])
        for one in found
    ] == [("standing", ["a", "b", "c"], [("a", "h1"), ("b", "h1"), ("c", "h4")])]


def test_alternative_attacks_on_the_case_host_are_not_a_disagreement():
    # The case's host is in every claim, and attacks on it are alternatives.
    found = find_disagreements(
        [
            account(
                "a",
                explanations=[
                    claim(
                        "a",
                        "h1",
                        "Credential dumping via procdump on FIN-WKS-014",
                        "proven",
                    )
                ],
            ),
            account(
                "b",
                explanations=[
                    claim("b", "h1", "Ransomware staging on FIN-WKS-014", "ruled_out"),
                    claim(
                        "b",
                        "h2",
                        "Data exfiltration to cloud storage from FIN-WKS-014",
                        "ruled_out",
                    ),
                ],
            ),
        ]
    )

    assert found == []


def test_run_local_refs_and_times_tie_nothing():
    gap = {
        "run_id": "a",
        "text": "splunk_search could not answer r7 at 2026-10-15T10:00:00Z (h1)",
        "kind": "visibility",
    }
    record = {
        "run_id": "b",
        "id": "r7.0",
        "claim": "r7 at 2026-10-15T10:00:00Z held h1",
    }

    assert (
        find_disagreements([account("a", gaps=[gap]), account("b", evidence=[record])])
        == []
    )


def test_one_shared_host_does_not_tie_a_gap_to_a_record():
    gap = {
        "run_id": "a",
        "text": "Did FIN-WKS-014 send anything out?",
        "kind": "open_question",
    }
    record = {"run_id": "b", "id": "r1.0", "claim": "FIN-WKS-014 ran a backup"}
    tied = {"run_id": "b", "id": "r2.0", "claim": "FIN-WKS-014 sent fin.7z out"}
    asked = {**gap, "text": "Did FIN-WKS-014 send fin.7z anywhere?"}

    assert (
        find_disagreements([account("a", gaps=[gap]), account("b", evidence=[record])])
        == []
    )
    found = find_disagreements(
        [account("a", gaps=[asked]), account("b", evidence=[record, tied])]
    )
    assert [(r.run_id, r.id) for r in found[0].refs or []] == [("b", "r2.0")]


def test_a_run_disputing_itself_is_not_a_gap_another_run_answers():
    gap = {"run_id": "a", "text": "FIN-WKS-014 fin.7z", "kind": "disputed"}
    record = {"run_id": "b", "id": "r1.0", "claim": "FIN-WKS-014 sent fin.7z out"}

    assert (
        find_disagreements([account("a", gaps=[gap]), account("b", evidence=[record])])
        == []
    )


def test_the_merge_carries_them_only_when_there_is_more_than_one_run():
    one = account("a", verdict=verdict("a", "malicious"))
    other = account("b", verdict=verdict("b", "benign"))

    assert merge_accounts([one]).disagreements is None
    assert merge_accounts([one, other]).disagreements[0].kind == "verdict"
    assert merge_accounts([one, account("c")]).disagreements == []


def test_the_nightshift_case_merges_to_its_golden():
    # The four runs of one case, oldest first, merged with their one disagreement.
    names = [
        "hunt-1e5c9131.json",
        "hunt-208b5901.json",
        "investigate-01c1d889.json",
        "root-cause-v2-nightshift.json",
    ]
    golden = json.loads((FIXTURES / "golden" / "case-nightshift.json").read_text())

    merged = merge_accounts([fixture(name) for name in names])

    assert dump_account(merged) == golden


def test_a_run_s_open_questions_are_one_disagreement_and_quoted_short():
    # A run whose searches all timed out leaves a question per search.
    asked = [
        {
            "run_id": "a",
            "text": f"Search {n} on fin.7z timed out. " + "x" * 200,
            "kind": "visibility",
        }
        for n in range(3)
    ]
    record = {"run_id": "b", "id": "r1.0", "claim": "FIN-WKS-014 sent fin.7z out"}
    other = {"run_id": "b", "id": "r2.0", "claim": "WEB-02 ran a backup"}

    found = find_disagreements(
        [account("a", gaps=asked), account("b", evidence=[record, other])]
    )

    assert len(found) == 1
    assert found[0].text.startswith(
        "investigate a could not see 3 things that investigate b holds records on:"
    )
    assert found[0].text.count("…") == 3
    assert [(r.run_id, r.id) for r in found[0].refs or []] == [("b", "r1.0")]


def test_a_run_s_blind_spots_and_questions_are_named_apart():
    gaps = [
        {"run_id": "a", "text": "Search on fin.7z timed out", "kind": "visibility"},
        {"run_id": "a", "text": "Was fin.7z sent out?", "kind": "open_question"},
    ]
    record = {"run_id": "b", "id": "r1.0", "claim": "FIN-WKS-014 sent fin.7z out"}
    other = {"run_id": "b", "id": "r2.0", "claim": "WEB-02 ran a backup"}

    found = find_disagreements(
        [account("a", gaps=gaps), account("b", evidence=[record, other])]
    )

    assert found[0].text.startswith(
        "investigate a could not see one thing and left one question open that"
    )
