"""The configuration fault cells on Helm, live: inject -> verify -> revert -> verify-clean, twice.

Opt-in (it brings up a kind cluster with Calico and all of Vigil, about 15 minutes).
Needs docker, kind, kubectl and helm, and the images e2e.sh full loads, built as
vigil-{backend,daemon,agent,medic,medic-gateway}:$TAG (TAG default kind):

    VIGIL_MEDIC_EVAL_LIVE=1 python -m pytest --noconftest -o addopts= tests/medic_eval/inject/test_helm_config_cells.py

The cluster is `medic-helm-cells` (CLUSTER overrides) and is deleted at the end.
VIGIL_MEDIC_HELM_CLUSTER names a cluster `CLUSTER=... KEEP=1 e2e.sh full` already left up:
it is used as it is and kept.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.medic_eval.inject.contract import run_cell
from tests.medic_eval.inject.helm_cells import CELLS
from tests.medic_eval.inject.helm_stack import E2E, HelmStack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("VIGIL_MEDIC_EVAL_LIVE") != "1",
        reason="live Helm test: set VIGIL_MEDIC_EVAL_LIVE=1",
    ),
    pytest.mark.skipif(
        not all(shutil.which(t) for t in ("docker", "kind", "kubectl", "helm")),
        reason="needs docker, kind, kubectl and helm",
    ),
]


@pytest.fixture(scope="module")
def stack():
    existing = os.environ.get("VIGIL_MEDIC_HELM_CLUSTER")
    cluster = existing or os.environ.get("CLUSTER", "medic-helm-cells")
    s = HelmStack(cluster)
    try:
        if not existing:
            subprocess.run(
                [str(E2E), "full"], env={**os.environ, "CLUSTER": cluster, "KEEP": "1"}, check=True
            )
        s.start()
        yield s
    finally:
        if not existing:
            s.stop()


@pytest.mark.parametrize("run", (1, 2))
@pytest.mark.parametrize("cell", CELLS, ids=lambda c: c.cause)
def test_cell_cycle(cell, run, stack, tmp_path):
    labels = run_cell(cell, stack, tmp_path)
    assert labels["fault"]["cause"] == cell.cause
