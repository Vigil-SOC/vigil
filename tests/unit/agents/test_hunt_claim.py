# The claim a report makes (#1918): its own stated hypothesis, verbatim, else the model's.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.memory.hunt_claim import model_claim, stated_hypothesis

pytestmark = pytest.mark.unit


BOTH = "Admin keys are minted off-hours. Then read."


@pytest.mark.parametrize(
    "report, claim",
    [
        (
            "## Hunting hypothesis\n\nAdmin keys are minted off-hours.\nThen read.\n\n## Next",
            BOTH,
        ),
        ("**Hunting hypothesis:** " + BOTH, BOTH),
        ("Hypothesis: " + BOTH, BOTH),
        ("Hunting hypothesis\n> Admin keys are minted off-hours.\n> Then read.", BOTH),
        (
            "Context.\nOur hypothesis is that Admin keys are minted off-hours. Then read.",
            "Admin keys are minted off-hours.",
        ),
    ],
)
def test_a_stated_hypothesis_is_taken_verbatim(report, claim):
    assert stated_hypothesis(report) == claim


def test_no_claim_in_a_plain_report():
    assert stated_hypothesis("C2 at 203.0.113.7, T1071.") is None
    assert stated_hypothesis("## Hunting hypothesis\n\n## Indicators\n- a") is None


def _model(monkeypatch, reply=None, error=None):
    monkeypatch.setattr(
        "core.llm.target.resolve_dispatch",
        lambda _c: (SimpleNamespace(provider_type="ollama"), "small"),
    )

    async def dispatch(self, **kwargs):
        if error:
            raise error
        return {"content": reply}

    monkeypatch.setattr("core.llm.router.router.LLMRouter.dispatch", dispatch)


async def test_model_claim_is_one_line(monkeypatch):
    _model(monkeypatch, reply='"A beacons\nto B."\n')

    assert await model_claim("report") == "A beacons to B."


async def test_model_claim_is_none_on_failure_or_no_model(monkeypatch):
    _model(monkeypatch, error=RuntimeError("down"))
    assert await model_claim("report") is None

    monkeypatch.setattr("core.llm.target.resolve_dispatch", lambda _c: None)
    assert await model_claim("report") is None
