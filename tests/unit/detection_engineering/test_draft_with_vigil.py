"""Draft with Vigil on the real database: the Case's Findings and technique reach the
model, its rule is an ordinary draft that lints like a pasted one, and asking again
while that draft is open returns it."""

import asyncio
from datetime import datetime, timezone

import pytest

from core.detections.engineering import drafting, proposals
from core.detections.engineering import siem_replay as sr

from .conftest import RULE


@pytest.fixture
def case_with_finding(add_case):
    from sqlalchemy import text

    from core.storage.models import Case, Finding, FindingMitrePrediction
    from core.storage.unit_of_work import unit_of_work

    case_id = add_case("case-de-draft")
    with unit_of_work() as session:
        session.execute(text("DELETE FROM findings WHERE finding_id LIKE 'f-de-%'"))
        finding = Finding(
            finding_id="f-de-draft",
            title="Word started encoded PowerShell",
            description="WINWORD.EXE -> powershell.exe -enc",
            data_source="elastic",
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            entity_context={"hostname": "host-case-de-draft"},
        )
        session.add(finding)
        session.flush()
        for technique, confidence in (("execution", 0.99), ("T1059.001", 0.9)):
            session.add(
                FindingMitrePrediction(
                    finding_id="f-de-draft",
                    technique_id=technique,
                    confidence=confidence,
                )
            )
        case = session.get(Case, case_id)
        case.findings.append(finding)
    yield case_id
    with unit_of_work() as session:
        session.execute(text("DELETE FROM findings WHERE finding_id LIKE 'f-de-%'"))


@pytest.fixture
def model(monkeypatch):
    asked = []

    async def complete(*, message, system_prompt):
        asked.append(message)
        return f"```yaml\n{RULE.strip()}\n```"

    monkeypatch.setattr(drafting.oneshot, "complete", complete)
    monkeypatch.setattr(sr, "configured_sources", lambda: ([], ["no SIEM in tests"]))
    return asked


def test_the_cases_findings_and_technique_reach_the_model(case_with_finding, model):
    view = asyncio.run(drafting.draft(case_with_finding, "gray"))
    (message,) = model
    assert "Finding: Word started encoded PowerShell" in message
    # A tactic name ranked first is no technique id; the first id is.
    assert "Technique: T1059.001" in message
    assert view["state"] == "draft" and view["technique"] == "T1059.001"
    assert view["case_id"] == case_with_finding
    assert view["origin"]["source"] == "model"


def test_the_draft_lints_like_a_pasted_one(case_with_finding, model):
    view = asyncio.run(drafting.draft(case_with_finding, "gray"))
    linted = proposals.lint(view["proposal_id"], "gray")
    assert linted["state"] == "linted"


def test_asking_again_returns_the_open_draft_until_it_is_rejected(
    case_with_finding, model
):
    first = asyncio.run(drafting.draft(case_with_finding, "gray"))
    again = asyncio.run(drafting.draft(case_with_finding, "gray"))
    assert again["proposal_id"] == first["proposal_id"] and len(model) == 1
    proposals.reject(first["proposal_id"], "gray", "Too broad; ask again.")
    fresh = asyncio.run(drafting.draft(case_with_finding, "gray"))
    assert fresh["proposal_id"] != first["proposal_id"] and len(model) == 2


def test_a_pasted_draft_on_the_case_does_not_stop_a_model_draft(
    case_with_finding, model
):
    proposals.create(sigma=RULE, actor="gray", case_id=case_with_finding)
    view = asyncio.run(drafting.draft(case_with_finding, "gray"))
    assert view["origin"]["source"] == "model" and len(model) == 1


def test_an_unknown_case_is_refused(db, model):
    from core.detections.overlay import Refused

    with pytest.raises(Refused):
        asyncio.run(drafting.draft("case-de-none", "gray"))
    assert model == []
