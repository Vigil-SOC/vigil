"""The host-native fault cells, live: inject -> verify -> revert -> verify-clean, twice.

Opt-in (it binds 127.0.0.1:6989, :6990 and ${DAEMON_HEALTH_PORT:-9091}, so it
must not run on a machine with a Vigil on it). scripts/medic/host-native/tests/
e2e_host_native.sh runs it on its throwaway runner:

    VIGIL_MEDIC_EVAL_HOST_NATIVE=1 python -m pytest --noconftest tests/medic_eval/inject/test_host_native_cells.py

Each cell's listeners are the only processes it ever signals.
"""

from __future__ import annotations

import os

import pytest

from tests.medic_eval.inject.contract import run_cell
from tests.medic_eval.inject.host_native_cells import CELLS, HostStack

pytestmark = pytest.mark.skipif(
    os.environ.get("VIGIL_MEDIC_EVAL_HOST_NATIVE") != "1",
    reason="binds Vigil's host ports: set VIGIL_MEDIC_EVAL_HOST_NATIVE=1",
)


@pytest.fixture(scope="module")
def stack():
    s = HostStack()
    yield s
    for port in s.procs:  # a failed run must not leave an orphan behind
        s.stop(port)


@pytest.mark.parametrize("run", (1, 2))
@pytest.mark.parametrize("cell", CELLS, ids=lambda c: c.cause)
def test_cell_cycle(cell, run, stack, tmp_path):
    labels = run_cell(cell, stack, tmp_path)
    assert labels["fault"]["cause"] == cell.cause
