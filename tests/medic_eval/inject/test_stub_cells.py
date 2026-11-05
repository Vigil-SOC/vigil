"""The third-party fault cells, live: inject -> verify -> revert -> verify-clean, twice.

Opt-in (it builds the backend, daemon and agent images and runs a stack for
several minutes):

    VIGIL_MEDIC_EVAL_LIVE=1 python -m pytest tests/medic_eval/inject/test_stub_cells.py

Each cell runs its cycle twice in a row on the stack in compose.eval.yml (project
`medic-eval`, or VIGIL_MEDIC_EVAL_PROJECT), and each run writes labels.json that
trace_check accepts. The project, its volumes and its images are removed at the
end; nothing outside it is touched.
"""

from __future__ import annotations

import os

import pytest

from tests.medic_compose.compose import HAVE_COMPOSE
from tests.medic_eval.inject.contract import run_cell
from tests.medic_eval.inject.stub_cells import CELLS
from tests.medic_eval.inject.stack import Stack

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
@pytest.mark.parametrize("cell", CELLS, ids=lambda c: c.cause)
def test_cell_cycle(cell, run, stack, tmp_path):
    labels = run_cell(cell, stack, tmp_path)
    assert labels["fault"]["cause"] == cell.cause
