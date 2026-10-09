"""A3-3: on Helm, Medic refuses to start where NetworkPolicy isn't enforced.

At start Medic tries one connection its own egress policy must block (by default
the backend Service, C3 R1). Only a timeout proves the policy dropped it:
connecting proves it isn't enforced, and a refusal or a name that won't resolve
proves nothing, so Medic refuses on both (fail closed).
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest

from services.medic.app import config
from services.medic.app.cli import POLICY_REFUSED, main
from services.medic.app.heartbeat import heartbeat_path
from services.medic.app.policy_probe import Verdict, probe
from services.medic.tests.fakes import FakeClock

HELM = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_INSTALL_SHAPE": "helm"}


def _connect_raising(exc: BaseException):
    def connect(addr, timeout):
        raise exc

    return connect


def _connect_ok(addr, timeout):
    class Conn:
        def close(self) -> None:
            pass

    return Conn()


def _resolve_ok(host, port):
    return [("10.96.0.10", port)]


def test_timeout_means_enforced() -> None:
    v = probe(
        ("backend", 6987), connect=_connect_raising(TimeoutError()), resolve=_resolve_ok
    )
    assert v == Verdict.BLOCKED
    assert v.enforced


def test_connected_means_not_enforced() -> None:
    v = probe(("backend", 6987), connect=_connect_ok, resolve=_resolve_ok)
    assert v == Verdict.CONNECTED
    assert not v.enforced


def test_refused_is_inconclusive_and_not_enforced() -> None:
    v = probe(
        ("backend", 6987),
        connect=_connect_raising(ConnectionRefusedError()),
        resolve=_resolve_ok,
    )
    assert v == Verdict.INCONCLUSIVE
    assert not v.enforced


def test_name_that_wont_resolve_is_inconclusive() -> None:
    def resolve(host, port):
        raise socket.gaierror("no such host")

    v = probe(("backend", 6987), connect=_connect_ok, resolve=resolve)
    assert v == Verdict.UNRESOLVED
    assert not v.enforced


def test_other_os_error_is_inconclusive() -> None:
    v = probe(
        ("backend", 6987),
        connect=_connect_raising(OSError(113, "No route to host")),
        resolve=_resolve_ok,
    )
    assert v == Verdict.INCONCLUSIVE


def test_probe_passes_its_timeout() -> None:
    seen: list[float] = []

    def connect(addr, timeout):
        seen.append(timeout)
        raise TimeoutError()

    probe(("backend", 6987), connect=connect, resolve=_resolve_ok, timeout=2.5)
    assert seen == [2.5]


def test_real_listener_connects() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        assert probe(srv.getsockname()) == Verdict.CONNECTED


# --- config ---------------------------------------------------------------


def test_probe_target_required_on_helm() -> None:
    with pytest.raises(config.ConfigError) as exc:
        config.policy_probe_addr(HELM, "helm")
    assert config.POLICY_PROBE_VAR in str(exc.value)


def test_probe_target_parsed_on_helm() -> None:
    env = {**HELM, config.POLICY_PROBE_VAR: "vigil-backend:6987"}
    assert config.policy_probe_addr(env, "helm") == ("vigil-backend", 6987)


@pytest.mark.parametrize("value", ["http://x:1", "x", "x:0", "x:70000", "u@x:1", " :1"])
def test_probe_target_must_be_host_port(value: str) -> None:
    env = {**HELM, config.POLICY_PROBE_VAR: value}
    with pytest.raises(config.ConfigError) as exc:
        config.policy_probe_addr(env, "helm")
    assert "value not shown" in str(exc.value)


@pytest.mark.parametrize("shape", ["compose", "start_sh"])
def test_no_probe_off_helm(shape: str) -> None:
    # Compose isolates by Docker network; host-native can't be isolated (C3 §4.7).
    env = {config.POLICY_PROBE_VAR: "backend:6987"}
    assert config.policy_probe_addr(env, shape) is None


# --- control target --------------------------------------------------------


def test_control_required_on_helm() -> None:
    env = {**HELM, config.POLICY_PROBE_VAR: "backend:6987"}
    with pytest.raises(config.ConfigError) as exc:
        config.policy_control_addr(env, "helm")
    assert config.POLICY_CONTROL_VAR in str(exc.value)


def test_control_parsed_on_helm_and_none_elsewhere() -> None:
    env = {**HELM, config.POLICY_CONTROL_VAR: "rel-vigil-medic-gateway:8471"}
    assert config.policy_control_addr(env, "helm") == ("rel-vigil-medic-gateway", 8471)
    assert config.policy_control_addr(env, "compose") is None


# --- run ------------------------------------------------------------------

TARGET = ("rel-vigil-backend", 6987)
CONTROL = ("rel-vigil-medic-gateway", 8471)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **HELM,
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
        config.POLICY_PROBE_VAR: "{}:{}".format(*TARGET),
        config.POLICY_CONTROL_VAR: "{}:{}".format(*CONTROL),
    }


def _run(tmp_path: Path, monkeypatch, caplog, verdicts: dict, waits=None) -> int:
    from services.medic.app import cli

    def probe(addr):
        v = verdicts[addr]
        return v.pop(0) if isinstance(v, list) else v

    monkeypatch.setattr(cli, "policy_probe", probe)
    monkeypatch.setattr(cli, "_control_wait", ([] if waits is None else waits).append)
    clock = FakeClock()
    with caplog.at_level(logging.INFO, logger="services.medic"):
        return main(
            ["run"], env=_env(tmp_path), clock=clock, sleep=clock.sleep, max_cycles=0
        )


def _wrote_nothing(tmp_path: Path) -> bool:
    return (
        not heartbeat_path(tmp_path).exists() and not (tmp_path / "medic.db").exists()
    )


def test_run_starts_when_control_connects_and_target_is_dropped(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    code = _run(
        tmp_path,
        monkeypatch,
        caplog,
        {CONTROL: Verdict.CONNECTED, TARGET: Verdict.BLOCKED},
    )
    assert code == 0
    assert "NetworkPolicy is enforced" in caplog.text
    assert heartbeat_path(tmp_path).exists()


def test_run_refuses_when_target_connects(tmp_path: Path, monkeypatch, caplog) -> None:
    code = _run(
        tmp_path,
        monkeypatch,
        caplog,
        {CONTROL: Verdict.CONNECTED, TARGET: Verdict.CONNECTED},
    )
    assert code == POLICY_REFUSED
    assert POLICY_REFUSED not in (0, 1, 2)
    assert "NetworkPolicy isn't enforced" in caplog.text and "A3-3" in caplog.text
    assert _wrote_nothing(tmp_path)


@pytest.mark.parametrize("verdict", [Verdict.INCONCLUSIVE, Verdict.UNRESOLVED])
def test_run_refuses_when_target_proves_nothing(
    tmp_path: Path, monkeypatch, caplog, verdict
) -> None:
    code = _run(
        tmp_path, monkeypatch, caplog, {CONTROL: Verdict.CONNECTED, TARGET: verdict}
    )
    assert code == POLICY_REFUSED
    assert "can't prove" in caplog.text and verdict.value in caplog.text
    assert _wrote_nothing(tmp_path)


@pytest.mark.parametrize(
    "control", [Verdict.BLOCKED, Verdict.INCONCLUSIVE, Verdict.UNRESOLVED]
)
def test_a_timeout_proves_nothing_unless_the_control_connects(
    tmp_path: Path, monkeypatch, caplog, control
) -> None:
    # Review #1: a dead pod network or an endpoint-less Service under IPVS drops
    # packets too. Only "the allowed path works, the forbidden one is dropped"
    # is evidence of a policy.
    code = _run(
        tmp_path, monkeypatch, caplog, {CONTROL: control, TARGET: Verdict.BLOCKED}
    )
    assert code == POLICY_REFUSED
    assert "control" in caplog.text and control.value in caplog.text
    assert _wrote_nothing(tmp_path)


def test_run_refuses_on_helm_without_a_target(tmp_path: Path, caplog) -> None:
    env = {**HELM, "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    clock = FakeClock()
    with caplog.at_level(logging.INFO, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == 1
    assert config.POLICY_PROBE_VAR in caplog.text
    assert _wrote_nothing(tmp_path)


def test_real_sockets_connected_target_refuses(tmp_path: Path, caplog) -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        addr = "{}:{}".format(*srv.getsockname())
        env = {
            **_env(tmp_path),
            config.POLICY_PROBE_VAR: addr,
            config.POLICY_CONTROL_VAR: addr,
        }
        clock = FakeClock()
        with caplog.at_level(logging.INFO, logger="services.medic"):
            code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == POLICY_REFUSED
    assert "isn't enforced" in caplog.text


def test_control_is_retried_while_the_gateway_starts(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # Medic usually starts before the gateway is Ready (re-review): retry the
    # control only, for a bounded time, then decide.
    from services.medic.app import cli

    waits: list[float] = []
    control = [Verdict.INCONCLUSIVE, Verdict.BLOCKED, Verdict.CONNECTED]
    code = _run(
        tmp_path,
        monkeypatch,
        caplog,
        {CONTROL: control, TARGET: Verdict.BLOCKED},
        waits,
    )
    assert code == 0
    assert waits == [cli.CONTROL_RETRY_S, cli.CONTROL_RETRY_S]


def test_control_retry_is_bounded(tmp_path: Path, monkeypatch, caplog) -> None:
    from services.medic.app import cli

    waits: list[float] = []
    code = _run(
        tmp_path,
        monkeypatch,
        caplog,
        {CONTROL: Verdict.INCONCLUSIVE, TARGET: Verdict.BLOCKED},
        waits,
    )
    assert code == POLICY_REFUSED
    assert len(waits) == cli.CONTROL_ATTEMPTS - 1
    assert cli.CONTROL_ATTEMPTS * cli.CONTROL_RETRY_S <= 120
