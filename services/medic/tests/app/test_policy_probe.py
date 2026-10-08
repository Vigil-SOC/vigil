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


# --- run ------------------------------------------------------------------


def test_run_refuses_when_policy_not_enforced(tmp_path: Path, caplog) -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        host, port = srv.getsockname()
        env = {
            **HELM,
            "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
            config.POLICY_PROBE_VAR: f"{host}:{port}",
        }
        clock = FakeClock()
        with caplog.at_level(logging.INFO, logger="services.medic"):
            code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == POLICY_REFUSED
    assert POLICY_REFUSED not in (0, 1, 2)
    text = caplog.text
    assert "NetworkPolicy" in text and "isn't enforced" in text
    assert "A3-3" in text
    # Refused before anything is written: no store, no beat, no gap.
    assert not heartbeat_path(tmp_path).exists()
    assert not (tmp_path / "medic.db").exists()


def test_run_refuses_on_inconclusive(tmp_path: Path, caplog) -> None:
    # A port just freed: connection refused. (Bound but not listening isn't the
    # same on macOS: the SYN is dropped, which reads as a policy drop.)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        host, port = srv.getsockname()
    env = {
        **HELM,
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        config.POLICY_PROBE_VAR: f"{host}:{port}",
    }
    clock = FakeClock()
    with caplog.at_level(logging.INFO, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == POLICY_REFUSED
    assert "can't prove" in caplog.text
    assert not (tmp_path / "medic.db").exists()


def test_run_refuses_on_helm_without_a_target(tmp_path: Path, caplog) -> None:
    env = {**HELM, "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    clock = FakeClock()
    with caplog.at_level(logging.INFO, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == 1
    assert config.POLICY_PROBE_VAR in caplog.text
    assert not (tmp_path / "medic.db").exists()


def test_run_starts_when_policy_blocks(tmp_path: Path, monkeypatch, caplog) -> None:
    from services.medic.app import cli

    monkeypatch.setattr(cli, "policy_probe", lambda target: Verdict.BLOCKED)
    env = {
        **HELM,
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        config.POLICY_PROBE_VAR: "backend:6987",
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
    }
    clock = FakeClock()
    with caplog.at_level(logging.INFO, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
    assert code == 0
    assert "NetworkPolicy is enforced" in caplog.text
    assert heartbeat_path(tmp_path).exists()
