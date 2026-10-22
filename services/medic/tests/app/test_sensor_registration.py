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
    # Readiness sensors carry a url. The Helm kube sensor does not.
    return {s.id: s.url for s in sensors_for(env, shape) if hasattr(s, "url")}


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


def test_the_kube_sensor_is_registered_on_helm_only() -> None:
    def ids(shape: str) -> list[str]:
        return [s.id for s in sensors_for({}, shape)]

    assert "kube" in ids("helm")
    assert "kube" not in ids("compose")
    assert "kube" not in ids("start_sh")
    kube = next(s for s in sensors_for({}, "helm") if s.id == "kube")
    assert not hasattr(kube, "url")
    assert kube.covers == ("pods", "version")
    assert kube.service == "medic"
    assert kube.interval_s == 30
    assert kube.timeout_s < kube.interval_s


@pytest.mark.parametrize(
    "value", ["http://agent-serve:6989/readyz", "u:hunter2secret@agent-serve:6989"]
)
def test_a_bad_serve_address_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ConfigError) as err:
        sensors_for({SERVE_VAR: value}, "compose")
    assert SERVE_VAR in str(err.value) and value not in str(err.value)
