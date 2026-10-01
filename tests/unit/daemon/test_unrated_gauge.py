"""soc_daemon_findings_unrated exports the two counts, and only those."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.metrics")

from services.daemon import orchestrator  # noqa: E402

pytestmark = pytest.mark.unit


def test_the_gauge_observes_the_counts_but_not_the_cap_flag(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_count_unrated_findings",
        lambda: {
            "waiting_to_be_rated": 3,
            "never_rated": 100_000,
            "never_rated_capped": True,
        },
    )

    observed = orchestrator._observe_unrated_findings(None)

    assert [(o.value, dict(o.attributes)) for o in observed] == [
        (3, {"state": "waiting_to_be_rated"}),
        (100_000, {"state": "never_rated"}),
    ]


def test_a_failed_count_observes_nothing(monkeypatch):
    def unreachable():
        raise RuntimeError("findings unreadable")

    monkeypatch.setattr(orchestrator, "_count_unrated_findings", unreachable)

    assert orchestrator._observe_unrated_findings(None) == []
