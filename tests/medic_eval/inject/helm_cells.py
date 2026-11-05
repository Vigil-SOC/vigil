"""The configuration fault cells on Helm (SW I2): the same six as config_cells.py.

The verify functions are config_cells.py's, unchanged, run on a HelmStack. So are
the injects that go through the API or `recreate` (I-1.b, I-4.b, I-5.b, L-1.c).
Only what Compose does with its environment is made here with kubectl: I-5.a on
the daemon StatefulSet and P-1.c on the agent Deployments. I-5.b pushes from the
backend pod, in the namespace: the chart's Ingress routes only to the backend.
P-3.a is left out: it needs a patched image.
"""

from __future__ import annotations

from dataclasses import replace

from tests.medic_eval.inject import config_cells
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.helm_stack import HelmStack


def i5a_inject(s: HelmStack) -> None:
    s.set_webhook_token("")


def i5a_revert(s: HelmStack) -> None:
    s.set_webhook_token(s.good_webhook_token)


def p1c_inject(s: HelmStack) -> None:
    s.set_agent_token("")


def p1c_revert(s: HelmStack) -> None:
    s.set_agent_token(None)  # the override goes; the chart Secret's token shows again


RESTART = "kubectl rollout restart statefulset/<release>-vigil-daemon"
_by = {c.cause: c for c in config_cells.CELLS}

CELLS = (
    replace(
        _by["I-1.b"],
        actions=(_by["I-1.b"].actions[0], RESTART),
        undo=(_by["I-1.b"].undo[0], RESTART),
    ),
    replace(_by["I-4.b"], undo=(RESTART,)),
    replace(
        _by["I-5.a"],
        inject=i5a_inject,
        revert=i5a_revert,
        actions=("kubectl set env statefulset/<release>-vigil-daemon DAEMON_WEBHOOK_TOKEN=",),
        undo=("kubectl set env statefulset/<release>-vigil-daemon DAEMON_WEBHOOK_TOKEN=<token>",),
    ),
    _by["I-5.b"],
    replace(
        _by["P-1.c"],
        inject=p1c_inject,
        revert=p1c_revert,
        actions=("kubectl set env deployment/<release>-vigil-agent-serve deployment/<release>-vigil-agent-worker AGENT_INTERNAL_TOKEN=",),
        undo=("kubectl set env deployment/<release>-vigil-agent-serve deployment/<release>-vigil-agent-worker AGENT_INTERNAL_TOKEN-",),
    ),
    _by["L-1.c"],
)
