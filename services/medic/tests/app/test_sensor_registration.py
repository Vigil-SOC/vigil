"""Which sensors `run` registers per install shape (S10, handoff from S6 L69).

agent-serve's `/readyz` (6989) is read on Compose, where agent-serve shares
`medic-net` with Medic. On Helm, Medic's egress policy (S7) has no rule for it, and
host-native is S11's: there it is registered only when the variable names it.
"""

from __future__ import annotations

import pytest

from services.medic.app.cli import sensors_for
from services.medic.app.config import ConfigError

SERVE_VAR = "VIGIL_MEDIC_AGENT_SERVE_ADDR"


def _urls(env: dict[str, str], shape: str) -> dict[str, str]:
    return {s.id: s.url for s in sensors_for(env, shape)}


def test_compose_reads_the_worker_and_serve() -> None:
    assert _urls({}, "compose") == {
        "http_ready.agent_worker": "http://agent-worker:6990/readyz",
        "http_ready.agent_serve": "http://agent-serve:6989/readyz",
    }


@pytest.mark.parametrize("shape", ["helm", "start_sh"])
def test_serve_isnt_read_where_medic_cant_reach_it_by_default(shape: str) -> None:
    assert list(_urls({}, shape)) == ["http_ready.agent_worker"]


@pytest.mark.parametrize("shape", ["compose", "helm", "start_sh"])
def test_the_variable_sets_where_serve_is_read(shape: str) -> None:
    urls = _urls({SERVE_VAR: "127.0.0.1:16989"}, shape)
    assert urls["http_ready.agent_serve"] == "http://127.0.0.1:16989/readyz"


@pytest.mark.parametrize(
    "value", ["http://agent-serve:6989/readyz", "u:hunter2secret@agent-serve:6989"]
)
def test_a_bad_serve_address_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ConfigError) as err:
        sensors_for({SERVE_VAR: value}, "compose")
    assert SERVE_VAR in str(err.value) and value not in str(err.value)
