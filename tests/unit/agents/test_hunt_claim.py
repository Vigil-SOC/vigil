# The claim a report makes (#1918): its own stated hypothesis, verbatim, else the model's.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.memory import hunt_claim
from core.memory.hunt_claim import claim_from, model_claim, stated_hypothesis

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


# As LiteParse extracts a PDF (#2279): no blank line between a section and the
# next title, so the next title is all that ends the hypothesis.
EXTRACTED = """DUSKY LANTERN: ISO/LNK phishing delivering a rundll32-loaded
beacon to finance staff
Executive summary
Since late September 2026 we have tracked DUSKY LANTERN, a financially motivated
intrusion set, sending invoice-themed spearphishing to accounts-payable staff.
Hunting hypothesis
A finance workstation that opened an invoice-themed ISO attachment has executed rundll32.exe from
a shortcut and is beaconing over HTTPS to DUSKY LANTERN infrastructure, and the operator has
since accessed LSASS memory on that host to harvest credentials.
Observed TTPs (MITRE ATT&CK)
Technique                                   ID            Observation
Spearphishing Attachment                    T1566.001     Invoice-themed email with ISO attachment
Indicators of compromise
Domain     cdn-invoicesync.net                                                  Beacon C2 (HTTPS)
"""


def test_a_stated_hypothesis_is_found_in_text_extracted_from_a_pdf():
    assert stated_hypothesis(EXTRACTED) == (
        "A finance workstation that opened an invoice-themed ISO attachment has "
        "executed rundll32.exe from a shortcut and is beaconing over HTTPS to DUSKY "
        "LANTERN infrastructure, and the operator has since accessed LSASS memory on "
        "that host to harvest credentials."
    )


def test_a_wrapped_line_that_is_short_does_not_end_the_section():
    # Mid-sentence, so a capitalised short line is the sentence going on.
    report = (
        "Hunting hypothesis\nAdmin keys are minted by\nOkta Workflows off-hours.\nNext"
    )
    assert (
        stated_hypothesis(report)
        == "Admin keys are minted by Okta Workflows off-hours."
    )


async def test_a_stated_section_over_the_cap_falls_through_to_the_model(monkeypatch):
    report = "Hunting hypothesis\n" + "The actor beacons out. " * 40 + "\nIndicators"
    assert stated_hypothesis(report) is None

    async def model(_report):
        return "A beacons to B."

    monkeypatch.setattr(hunt_claim, "model_claim", model)
    assert await claim_from(report) == "A beacons to B."


def _model(monkeypatch, reply=None, error=None, finish="stop", sent=None):
    monkeypatch.setattr(
        "core.llm.target.resolve_dispatch",
        lambda _c: (SimpleNamespace(provider_type="ollama"), "small"),
    )
    monkeypatch.setattr("core.llm.target.resolve_effort", lambda _c: "high")

    async def dispatch(self, **kwargs):
        if sent is not None:
            sent.update(kwargs)
        if error:
            raise error
        return {"content": reply, "finish_reason": finish}

    monkeypatch.setattr("core.llm.router.router.LLMRouter.dispatch", dispatch)


async def test_model_claim_is_one_line(monkeypatch):
    _model(monkeypatch, reply='"A beacons\nto B."\n')

    assert await model_claim("report") == "A beacons to B."


async def test_model_claim_gives_a_reasoning_model_room_at_its_effort(monkeypatch):
    sent = {}
    _model(monkeypatch, reply="A beacons to B.", sent=sent)

    assert await model_claim("report") == "A beacons to B."
    assert sent["max_tokens"] >= 4_096
    assert sent["effort"] == "high"


@pytest.mark.parametrize(
    "reply, finish, said",
    [
        ("", "length", "ran out of tokens"),
        ("A finance workstation that", "length", "ran out of tokens"),
        ("  ", "stop", "replied with nothing"),
        (None, "stop", "replied with nothing"),
    ],
)
async def test_a_reply_cut_off_or_empty_is_a_logged_failure(
    monkeypatch, caplog, reply, finish, said
):
    _model(monkeypatch, reply=reply, finish=finish)

    with caplog.at_level("WARNING", logger="core.memory.hunt_claim"):
        assert await model_claim("report") is None
    assert said in caplog.text


async def test_model_claim_is_none_on_failure_or_no_model(monkeypatch):
    _model(monkeypatch, error=RuntimeError("down"))
    assert await model_claim("report") is None

    monkeypatch.setattr("core.llm.target.resolve_dispatch", lambda _c: None)
    assert await model_claim("report") is None
