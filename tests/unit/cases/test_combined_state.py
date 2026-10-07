"""Combined state and the 75 / 90 health cuts."""

from datetime import datetime, timedelta

from core.cases.combined_state import (
    budget_health,
    combined_state,
    health_word,
    sla_clock,
)

NOW = datetime(2026, 6, 15, 12, 0, 0)


def test_closed_case_stays_closed_even_with_a_live_investigation():
    assert combined_state("closed", "executing") == "closed"


def test_live_investigation_contributes_its_status():
    assert combined_state("investigating", "executing") == "executing"
    assert combined_state("open", "waiting_approval") == "waiting_approval"
    assert combined_state("open", "review_submitted") == "review_submitted"


def test_a_finished_investigation_leaves_the_case_status():
    assert combined_state("open", "completed") == "open"
    assert combined_state("investigating", None) == "investigating"


def test_health_word_uses_the_sla_cuts():
    assert health_word(0.749) == "healthy"
    assert health_word(0.75) == "warning"
    assert health_word(0.899) == "warning"
    assert health_word(0.90) == "critical"
    assert health_word(1.2) == "critical"


def test_budget_health_is_cost_over_max_on_those_cuts():
    assert budget_health(1.0, 5.0) == "healthy"
    assert budget_health(3.75, 5.0) == "warning"
    assert budget_health(4.5, 5.0) == "critical"
    assert budget_health(1.0, 0) is None
    assert budget_health(None, 5.0) is None


def test_sla_clock_matches_the_service_rules():
    created = NOW - timedelta(hours=80)
    health, left = sla_clock(
        now=NOW,
        has_sla=True,
        sla_created_at=created,
        response_due=NOW + timedelta(hours=100),
        resolution_due=created + timedelta(hours=100),
        response_completed_at=NOW,
        resolution_completed_at=None,
        is_paused=False,
    )
    assert health == "warning"  # 80%
    assert left == timedelta(hours=20).total_seconds()

    breached, _ = sla_clock(
        now=NOW,
        has_sla=True,
        sla_created_at=NOW - timedelta(hours=2),
        response_due=NOW + timedelta(hours=10),
        resolution_due=NOW - timedelta(hours=1),
        response_completed_at=NOW,
        resolution_completed_at=None,
        is_paused=False,
    )
    assert breached == "breached"

    paused, paused_left = sla_clock(
        now=NOW,
        has_sla=True,
        sla_created_at=NOW - timedelta(hours=99),
        response_due=NOW,
        resolution_due=NOW - timedelta(hours=1),
        response_completed_at=None,
        resolution_completed_at=None,
        is_paused=True,
    )
    assert paused == "healthy"
    assert paused_left is None

    assert sla_clock(
        now=NOW,
        has_sla=False,
        sla_created_at=None,
        response_due=None,
        resolution_due=None,
        response_completed_at=None,
        resolution_completed_at=None,
        is_paused=False,
    ) == (None, None)
