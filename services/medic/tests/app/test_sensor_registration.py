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
PROXY_VAR = "VIGIL_MEDIC_DOCKERPROXY_ADDR"


def _urls(env: dict[str, str], shape: str) -> dict[str, str]:
    # Container state has no URL: it speaks the proxy's host:port, not /readyz.
    return {
        sensor.id: sensor.url
        for sensor in sensors_for(env, shape)
        if getattr(sensor, "url", None) is not None
    }


def _proxy(env: dict[str, str], shape: str) -> list:
    return [
        sensor for sensor in sensors_for(env, shape) if sensor.id == "docker.containers"
    ]


def test_compose_reads_the_worker_and_serve() -> None:
    assert _urls({}, "compose") == {
        "http_ready.agent_worker": "http://agent-worker:6990/readyz",
        "http_ready.agent_serve": "http://agent-serve:6989/readyz",
    }


@pytest.mark.parametrize("shape", ["helm", "start_sh"])
def test_serve_isnt_read_where_medic_cant_reach_it_by_default(shape: str) -> None:
    assert list(_urls({}, shape)) == ["http_ready.agent_worker"]
    assert _proxy({}, shape) == []


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


def test_compose_reads_container_state_through_the_proxy() -> None:
    (sensor,) = _proxy({}, "compose")
    assert (sensor.host, sensor.port) == ("medic-dockerproxy", 8472)


def test_the_variable_sets_the_proxy_address() -> None:
    (sensor,) = _proxy({PROXY_VAR: "127.0.0.1:18472"}, "compose")
    assert (sensor.host, sensor.port) == ("127.0.0.1", 18472)


@pytest.mark.parametrize("shape", ["helm", "start_sh"])
def test_container_state_is_compose_only(shape: str) -> None:
    # Set or not, Helm and host-native do not register this sensor.
    assert _proxy({PROXY_VAR: "127.0.0.1:18472"}, shape) == []


@pytest.mark.parametrize(
    "value",
    [
        "http://medic-dockerproxy:8472/containers/json",
        "u:hunter2secret@medic-dockerproxy:8472",
    ],
)
def test_a_bad_proxy_address_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ConfigError) as err:
        sensors_for({PROXY_VAR: value}, "compose")
    assert PROXY_VAR in str(err.value) and value not in str(err.value)
