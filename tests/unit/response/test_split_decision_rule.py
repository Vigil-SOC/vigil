"""split_decision_rule is the inverse of decision_rule (#917, #2052)."""

import pytest

from core.response.config import decision_rule, split_decision_rule

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("x; approval.human_only=True", ("x", "approval.human_only=True")),
        (
            "x; response.confidence_threshold=0.90 met (0.92)",
            ("x", "response.confidence_threshold=0.90 met (0.92)"),
        ),
        (
            "x; response.confidence_threshold=0.90 not met (0.40)",
            ("x", "response.confidence_threshold=0.90 not met (0.40)"),
        ),
        ("x; reversibility=irreversible", ("x", "reversibility=irreversible")),
        ("No rule here", ("No rule here", None)),
        ("Isolate host; then review the ticket", ("Isolate host; then review the ticket", None)),
        ("a; b; approval.human_only=True", ("a; b", "approval.human_only=True")),
        ("approval.human_only=True", ("", "approval.human_only=True")),
        ("", ("", None)),
        (None, ("", None)),
    ],
)
def test_split(reason, expected):
    assert split_decision_rule(reason) == expected


def test_round_trips_what_decision_rule_emits():
    rule = decision_rule("response.confidence_threshold", 0.9, 0.92)
    assert split_decision_rule(f"why; {rule}") == ("why", rule)
