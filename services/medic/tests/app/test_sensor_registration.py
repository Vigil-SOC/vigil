"""Which sensors `run` registers per install shape (S10, handoff from S6 L69).

agent-serve's `/readyz` (6989) is read on Compose, where agent-serve shares
`medic-net` with Medic. On Helm, Medic's egress policy (S7) has no rule for it, and
host-native is S11's: there it is registered only when the variable names it.
"""

from __future__ import annotations

import logging

import pytest

from services.medic.app.cli import main, sensors_for
from services.medic.app.config import DEV_MODE_VAR, GATEWAY_VAR, ConfigError, dev_mode
from services.medic.sensors.base import SensorContext
from services.medic.sensors.bus import Bus, Stats
from services.medic.sensors.scheduler import Scheduler
from services.medic.tests.fakes import FakeClock

SERVE_VAR = "VIGIL_MEDIC_AGENT_SERVE_ADDR"
PROXY_VAR = "VIGIL_MEDIC_DOCKERPROXY_ADDR"
# Helm has no default: the chart always sets this. Tests that aren't about the
# gateway still have to name one, or sensors_for refuses to start.
HELM_GATEWAY = "vigil-medic-gateway:8471"


def _env(shape: str, **extra: str) -> dict[str, str]:
    env = dict(extra)
    if shape == "helm":
        env.setdefault(GATEWAY_VAR, HELM_GATEWAY)
    return env


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
    assert list(_urls(_env(shape), shape)) == ["http_ready.agent_worker"]
    assert _proxy(_env(shape), shape) == []


@pytest.mark.parametrize("shape", ["compose", "helm", "start_sh"])
def test_the_variable_sets_where_serve_is_read(shape: str) -> None:
    urls = _urls(_env(shape, **{SERVE_VAR: "127.0.0.1:16989"}), shape)
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
    assert _proxy(_env(shape, **{PROXY_VAR: "127.0.0.1:18472"}), shape) == []


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


def _gateway(env: dict[str, str], shape: str):
    return [s for s in sensors_for(env, shape) if s.id == "gateway_session"]


def test_compose_reads_the_gateway_on_medic_private() -> None:
    (sensor,) = _gateway({}, "compose")
    assert (sensor.host, sensor.port) == ("medic-gateway-out", 8471)
    assert sensor.uses_vigil_api is True


def test_the_variable_sets_the_gateway_address() -> None:
    (sensor,) = _gateway({GATEWAY_VAR: "127.0.0.1:18471"}, "compose")
    assert (sensor.host, sensor.port) == ("127.0.0.1", 18471)


def test_start_sh_has_no_gateway_until_one_is_named() -> None:
    assert _gateway({}, "start_sh") == []
    (sensor,) = _gateway({GATEWAY_VAR: "127.0.0.1:18471"}, "start_sh")
    assert (sensor.host, sensor.port) == ("127.0.0.1", 18471)


def test_helm_requires_the_gateway_address() -> None:
    with pytest.raises(ConfigError) as err:
        sensors_for({}, "helm")
    assert GATEWAY_VAR in str(err.value)
    (sensor,) = _gateway({GATEWAY_VAR: HELM_GATEWAY}, "helm")
    assert (sensor.host, sensor.port) == ("vigil-medic-gateway", 8471)


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("compose", "http://medic-gateway-out:8471/api/health"),
        ("helm", "u:hunter2secret@medic-gateway-out:8471"),
        ("start_sh", "medic-gateway-out:70000"),
    ],
)
def test_a_bad_gateway_address_is_refused_without_echoing_it(
    shape: str, value: str
) -> None:
    with pytest.raises(ConfigError) as err:
        sensors_for({GATEWAY_VAR: value}, shape)
    assert GATEWAY_VAR in str(err.value) and value not in str(err.value)


def test_dev_mode_turns_gateway_session_off() -> None:
    sensors = sensors_for({}, "compose")
    sched = Scheduler(
        sensors,
        SensorContext(shape="compose", vigil_dev_mode=True),
        Bus(Stats()),
        FakeClock(),
    )
    assert sched.status()["off"]["gateway_session"] == "vigil_dev_mode"
    assert sched.status()["vigil_dev_mode"] is True


def test_run_with_dev_mode_leaves_gateway_session_off(tmp_path, caplog) -> None:
    clock = FakeClock()
    env = {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        "DEV_MODE": "true",
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
        "VIGIL_MEDIC_AGENT_SERVE_ADDR": "127.0.0.1:9",
        "VIGIL_MEDIC_DOCKERPROXY_ADDR": "127.0.0.1:9",
        "VIGIL_MEDIC_GATEWAY_ADDR": "127.0.0.1:9",
    }
    with caplog.at_level(logging.WARNING, logger="services.medic.sensors"):
        assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0) == 0
    assert "gateway_session is off" in caplog.text


def test_start_sh_without_a_gateway_still_starts(tmp_path) -> None:
    clock = FakeClock()
    env = {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        "VIGIL_MEDIC_INSTALL_SHAPE": "start_sh",
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
    }
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0) == 0


@pytest.mark.parametrize(
    "value", [None, "", "false", "False", "0", "no", "off", "f", "n"]
)
def test_dev_mode_off_values(value: str | None) -> None:
    env = {} if value is None else {DEV_MODE_VAR: value}
    assert dev_mode(env) is False


@pytest.mark.parametrize("value", ["true", "TRUE", " yes ", "1", "on", "maybe"])
def test_dev_mode_on_values(value: str) -> None:
    assert dev_mode({DEV_MODE_VAR: value}) is True
