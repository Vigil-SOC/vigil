"""The directive envelope Python writes is the one TypeScript reads back (GH #646)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.directives import (  # noqa: E402
    DIRECTIVE_KINDS,
    InvalidDirective,
    build_directive,
)

pytestmark = pytest.mark.unit


def test_carries_the_envelope_the_drain_reads():
    directive = build_directive(
        "note", "the 03:00 spike is our backup window", "analyst@example.com"
    )

    # Every field the TypeScript Directive requires, and origin marking it as an
    # operator's rather than the controller's own voice.
    assert set(directive) == {
        "directive_id",
        "actor",
        "kind",
        "text",
        "created_at",
        "origin",
    }
    assert directive["directive_id"].startswith("dir-")
    assert directive["origin"] == "inbox"
    assert directive["actor"] == "analyst@example.com"


def test_refuses_a_kind_the_drain_could_not_apply():
    with pytest.raises(InvalidDirective, match="unknown directive kind aprove"):
        build_directive("aprove", "looks right", "analyst")


def test_refuses_a_directive_nobody_owns():
    # The ledger's job is to say who steered a run; an unattributed directive
    # defeats that, so it is refused here rather than journaled.
    with pytest.raises(InvalidDirective, match="needs an actor"):
        build_directive("note", "someone said something", "")


def test_refuses_a_field_the_workflow_never_reads():
    with pytest.raises(InvalidDirective, match="unknown directive fields: checkpoint"):
        build_directive("approve", "yes", "analyst", {"checkpoint": "chk-1"})


def test_carries_the_fields_a_workflow_owns():
    directive = build_directive(
        "approve", "reviewed", "analyst", {"checkpoint_id": "chk-h1"}
    )
    assert directive["checkpoint_id"] == "chk-h1"


def test_kinds_match_the_typescript_vocabulary():
    """DIRECTIVE_KINDS is declared in two languages, so drift is the failure mode."""
    declared = (
        REPO / "services" / "agent" / "workflows" / "hunt" / "types.ts"
    ).read_text()
    block = declared.split("export const DIRECTIVE_KINDS = [")[1].split("]")[0]
    assert sorted(DIRECTIVE_KINDS) == sorted(
        line.strip().strip(',"') for line in block.strip().splitlines()
    )


# `extend ""` parsed to no grant, journaled a note saying so, and left the hunt parked
# at the ceiling it was asking to be let past. One run spent 7 of 7 iterations with
# $14.50 unspent that way and then concluded with nothing proven, so the amount is
# typed now rather than parsed out of prose.
def test_carries_a_typed_grant_an_extend_can_act_on():
    directive = build_directive(
        "extend",
        "",
        "analyst",
        {"grant": {"iterations": 3, "cost_usd": 0, "wall_ms": 0}},
    )

    assert directive["grant"] == {"iterations": 3.0, "cost_usd": 0.0, "wall_ms": 0.0}


def test_fills_in_the_arms_a_grant_leaves_out():
    directive = build_directive("extend", "", "analyst", {"grant": {"cost_usd": 5}})

    assert directive["grant"] == {"iterations": 0.0, "cost_usd": 5.0, "wall_ms": 0.0}


def test_refuses_a_grant_that_buys_nothing():
    # Silently doing nothing is the failure this replaced: an extension that grants
    # nothing leaves the hunt exactly where it was, and the operator none the wiser.
    with pytest.raises(InvalidDirective, match="grants nothing"):
        build_directive("extend", "", "analyst", {"grant": {"iterations": 0}})


@pytest.mark.parametrize("asked", [float("nan"), float("inf"), -1])
def test_refuses_a_grant_no_ceiling_could_survive(asked):
    # A ceiling is arithmetic: max_iterations + NaN is NaN, and `used >= NaN` is always
    # false, which is a hunt running with no ceiling at all.
    with pytest.raises(InvalidDirective, match="iterations"):
        build_directive("extend", "", "analyst", {"grant": {"iterations": asked}})


@pytest.mark.parametrize("asked", ["3", True, None, [3]])
def test_refuses_a_grant_arm_that_is_not_a_number(asked):
    with pytest.raises(InvalidDirective, match="must be a number"):
        build_directive("extend", "", "analyst", {"grant": {"cost_usd": asked}})


def test_refuses_a_grant_that_is_not_a_grant():
    with pytest.raises(InvalidDirective, match="how many iterations"):
        build_directive("extend", "", "analyst", {"grant": "a bit more"})


# Whole things are counted whole, the same way the prose parser floors them.
def test_floors_the_arms_that_count_whole_things():
    directive = build_directive(
        "extend", "", "analyst", {"grant": {"iterations": 2.9, "cost_usd": 5.5}}
    )

    assert directive["grant"]["iterations"] == 2.0
    assert directive["grant"]["cost_usd"] == 5.5
