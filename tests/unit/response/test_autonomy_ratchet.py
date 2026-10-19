"""The autonomy ratchet tightens on disagreement and waits for a person to loosen it."""

from datetime import datetime, timedelta

import pytest

from core.response.autonomy_ratchet import (
    DISAGREE_RATE,
    MIN_GRADED,
    MIN_REJECTIONS,
    DemotionWrite,
    EnvironmentHolds,
    Grade,
    NotHolding,
    ProposalRejection,
    StoredRatchet,
    demotion_write,
    judge,
    promotion_write,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 19, 18, 0, 0)
THRESHOLD = 0.85


def grade(decision: str, *, hours_ago: float = 1, decision_id: str = "d") -> Grade:
    return Grade(
        decision_id=decision_id,
        human_decision=decision,
        at=NOW - timedelta(hours=hours_ago),
        summary="isolate the host",
    )


def rejection(
    *,
    hours_ago: float = 1,
    confidence: float = 0.91,
    rejected_by: str = "ada",
    action_id: str = "a",
) -> ProposalRejection:
    return ProposalRejection(
        action_id=action_id,
        title="Isolate host",
        confidence=confidence,
        at=NOW - timedelta(hours=hours_ago),
        rejected_by=rejected_by,
    )


def test_two_disagrees_in_four_grades_tighten():
    verdict = judge(
        [
            grade("disagree", decision_id="d1"),
            grade("disagree", decision_id="d2", hours_ago=2),
            grade("agree", decision_id="d3", hours_ago=3),
            grade("agree", decision_id="d4", hours_ago=4),
        ],
        [],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    assert verdict.tighten
    assert verdict.graded == MIN_GRADED
    assert verdict.disagreements == 2
    assert "2 of 4" in verdict.reason
    assert [item.id for item in verdict.evidence] == ["d1", "d2"]


def test_one_disagree_stays_quiet():
    verdict = judge(
        [
            grade("disagree", decision_id="d1"),
            grade("agree", decision_id="d2"),
            grade("agree", decision_id="d3"),
            grade("agree", decision_id="d4"),
        ],
        [],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    assert not verdict.tighten
    assert verdict.reason == ""


def test_partial_weighs_half_so_two_partials_do_not_clear_the_rate():
    verdict = judge(
        [
            grade("partial", decision_id="d1"),
            grade("partial", decision_id="d2"),
            grade("agree", decision_id="d3"),
            grade("agree", decision_id="d4"),
        ],
        [],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    assert not verdict.tighten
    assert verdict.disagreements == 2
    # Two halves over four grades is under the rate, which is the gate.
    assert DISAGREE_RATE == 0.40


def test_grades_older_than_the_window_or_before_a_promotion_do_not_count():
    promoted = NOW - timedelta(hours=3)
    verdict = judge(
        [
            grade("disagree", decision_id="fresh"),
            grade("disagree", decision_id="before-promotion", hours_ago=4),
            grade("disagree", decision_id="stale", hours_ago=30),
            grade("agree", decision_id="also-fresh", hours_ago=1),
        ],
        [],
        now=NOW,
        promoted_at=promoted,
        review_threshold=THRESHOLD,
    )

    assert not verdict.tighten
    assert verdict.graded == 2
    assert verdict.disagreements == 1


def test_two_review_line_rejections_tighten_and_machine_rejections_do_not():
    verdict = judge(
        [],
        [
            rejection(action_id="a1"),
            rejection(action_id="a2", hours_ago=2, rejected_by="Analyst"),
            rejection(action_id="low", confidence=0.5),
            rejection(action_id="expired", rejected_by="system"),
            rejection(action_id="agent", rejected_by="agent"),
            rejection(action_id="blank", rejected_by=""),
        ],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    assert verdict.tighten
    assert verdict.rejections == MIN_REJECTIONS
    assert {item.id for item in verdict.evidence} == {"a1", "a2"}


def test_a_hold_is_not_tightened_again_and_a_quiet_window_writes_nothing():
    holding = StoredRatchet(holding=True, reason="already")
    loud = judge(
        [grade("disagree", decision_id=f"d{i}", hours_ago=i) for i in range(1, 5)],
        [],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    assert demotion_write(loud, holding, approval_enabled=False, now=NOW) is None
    quiet = judge([], [], now=NOW, promoted_at=None, review_threshold=THRESHOLD)
    assert (
        demotion_write(quiet, StoredRatchet(), approval_enabled=False, now=NOW) is None
    )


def test_a_demotion_turns_assist_on_only_when_it_was_off():
    verdict = judge(
        [],
        [rejection(action_id="a1"), rejection(action_id="a2")],
        now=NOW,
        promoted_at=None,
        review_threshold=THRESHOLD,
    )

    first = demotion_write(verdict, StoredRatchet(), approval_enabled=False, now=NOW)
    assert isinstance(first, DemotionWrite)
    assert first.enable_force_approval
    assert first.stored.holding
    assert first.stored.tightened_approval
    assert first.stored.demoted_at == NOW.isoformat(timespec="seconds")

    already = demotion_write(verdict, StoredRatchet(), approval_enabled=True, now=NOW)
    assert already is not None
    assert not already.enable_force_approval
    assert not already.stored.tightened_approval


def test_promotion_clears_the_hold_and_refuses_to_invent_one():
    held = StoredRatchet(
        holding=True, tightened_approval=True, demoted_at="2026-10-19T12:00:00"
    )

    write = promotion_write(
        held,
        environment_wins=False,
        actor="ada",
        reason="Reviewed the two isolates; both were drills.",
        now=NOW,
    )

    assert write.clear_force_approval
    assert not write.stored.holding
    assert write.stored.promoted_by == "ada"
    assert write.stored.promoted_at == NOW
    assert not write.stored.tightened_approval

    with pytest.raises(NotHolding):
        promotion_write(
            StoredRatchet(),
            environment_wins=False,
            actor="ada",
            reason="No hold to release.",
            now=NOW,
        )


def test_promotion_refuses_to_restore_act_while_the_environment_forces_approval():
    held = StoredRatchet(holding=True, tightened_approval=True)

    with pytest.raises(EnvironmentHolds):
        promotion_write(
            held,
            environment_wins=True,
            actor="ada",
            reason="The environment still wins.",
            now=NOW,
        )


def test_acknowledging_a_hold_that_did_not_flip_assist_leaves_the_flag_alone():
    held = StoredRatchet(holding=True, tightened_approval=False)

    write = promotion_write(
        held,
        environment_wins=False,
        actor="ada",
        reason="Assist was already our choice.",
        now=NOW,
    )

    assert not write.clear_force_approval
    assert not write.stored.holding
