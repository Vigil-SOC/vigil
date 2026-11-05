"""Medic survives its dependencies' outages and Viewer revocation, live (#3740).

Opt-in (it builds the images and runs a stack with Medic for many minutes):

    VIGIL_MEDIC_EVAL_LIVE=1 python -m pytest -s tests/medic_eval/inject/test_survival_checks.py

Each check runs twice in a row on the stack in compose.eval.yml with Medic and
auth on, and the dependency and Medic are healthy after each. The negative
control stops Medic itself for the window and the same liveness predicate must
fail. These write no labels.json; incidents Medic opens during an outage are
printed, not scored.
"""

from __future__ import annotations

import os

import pytest

from tests.medic_compose.compose import HAVE_COMPOSE
from tests.medic_eval.inject.stack import Stack
from tests.medic_eval.inject.survival_checks import (
    assert_alive,
    check_dependency_down,
    check_viewer_revoked,
    run_outage,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("VIGIL_MEDIC_EVAL_LIVE") != "1",
        reason="live Compose test: set VIGIL_MEDIC_EVAL_LIVE=1",
    ),
    pytest.mark.skipif(not HAVE_COMPOSE, reason="needs Docker with Compose v2"),
]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("medic-eval-home"))
    try:
        s.start()
        yield s
    finally:
        s.stop()


@pytest.mark.parametrize("run", (1, 2))
@pytest.mark.parametrize("service", ("postgres", "redis", "bifrost"))
def test_medic_survives_dependency_down(service, run, stack):
    print(check_dependency_down(stack, service))


@pytest.mark.parametrize("run", (1, 2))
def test_medic_survives_viewer_revoked(run, stack):
    print(check_viewer_revoked(stack))


def test_negative_control_stopped_medic_fails_liveness(stack):
    outage = run_outage(stack, "medic")
    with pytest.raises(AssertionError, match="liveness"):
        assert_alive(outage)
