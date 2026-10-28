"""One sentence per trail entry, from the server (#3308)."""

import pytest

from core.audit.words import sentence

pytestmark = pytest.mark.unit


def test_known_actions_read_as_words():
    assert sentence({"action": "case.comment", "case_id": "c-1"}) == (
        "Commented on the case"
    )
    assert sentence(
        {"action": "config.change", "verb": "update", "config_key": "llm"}
    ) == ("Changed setting llm")
    assert sentence({"action": "agent.tool.outcome", "tool": "splunk_search"}) == (
        "Called splunk_search"
    )
    assert sentence(
        {"action": "approval.approve", "outcome": "denied", "reason": "four_eyes"}
    ) == ("Was refused approving an action (four-eyes)")


def test_an_unknown_action_is_its_key_and_a_collapsed_one_counts():
    assert sentence({"action": "made.up"}) == "made.up"
    assert (
        sentence(
            {
                "action": "auth.sign_in_failed",
                "reason": "bad_password",
                "collapsed": {"count": 4},
            }
        )
        == "Failed to sign in: bad password (4 times)"
    )
