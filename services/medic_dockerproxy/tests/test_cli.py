"""`python -m services.medic_dockerproxy run | check`: configuration and health."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from services.medic_dockerproxy import cli
from services.medic_dockerproxy.tests.conftest import Harness


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close()


@pytest.fixture
def cfg_env(harness):
    return {
        "VIGIL_MEDIC_DOCKERPROXY_BIND": f"localhost:{free_port()}",
        "VIGIL_MEDIC_DOCKERPROXY_SOCKET": harness.docker.path,
    }


@pytest.mark.parametrize(
    "name",
    [
        "AGENT_INTERNAL_TOKEN",
        "VIGIL_TOOLS_TOKEN",
        "DAEMON_WEBHOOK_TOKEN",
        "VIGIL_MEDIC_API_KEY",
        "POSTGRES_PASSWORD",
    ],
)
def test_refuses_a_credential_in_its_environment(cfg_env, capsys, name):
    cfg_env[name] = "s3cr3t-value"
    assert cli.main(["run"], env=cfg_env) == 2
    err = capsys.readouterr().err
    assert name in err and "s3cr3t-value" not in err


@pytest.mark.parametrize(
    "bind",
    [None, "", "proxy", "proxy:http", "0.0.0.0:8472", ":8472", "[::]:8472", "0:8472"]
    + ["0.0:8472", "0x0:8472"],
)
def test_bad_bind_exits_2(cfg_env, capsys, bind):
    if bind is None:
        cfg_env.pop("VIGIL_MEDIC_DOCKERPROXY_BIND")
    else:
        cfg_env["VIGIL_MEDIC_DOCKERPROXY_BIND"] = bind
    assert cli.main(["run"], env=cfg_env) == 2
    assert "medic-dockerproxy:" in capsys.readouterr().err


def test_socket_defaults_to_the_standard_path(cfg_env):
    cfg_env.pop("VIGIL_MEDIC_DOCKERPROXY_SOCKET")
    assert cli.load_config(cfg_env).socket == "/var/run/docker.sock"


@pytest.mark.parametrize("argv", [[], ["serve"], ["run", "x"]])
def test_usage(argv, capsys):
    assert cli.main(argv, env={}) == 2
    assert "usage" in capsys.readouterr().err


def test_check_fails_when_nothing_listens(cfg_env):
    assert cli.main(["check"], env=cfg_env) == 1


def test_run_then_check_end_to_end(cfg_env, harness, capsys):
    """`check` goes through the proxy to Docker's /_ping: green only if both work."""
    stop = threading.Event()
    t = threading.Thread(
        target=cli.main, args=(["run"],), kwargs={"env": cfg_env, "stop": stop}
    )
    t.start()
    try:
        deadline = time.monotonic() + 5
        while cli.main(["check"], env=cfg_env) != 0:
            assert time.monotonic() < deadline, capsys.readouterr()
            time.sleep(0.05)
        assert any(x.startswith(b"GET /_ping HTTP/1.1") for x in harness.docker.heads)
    finally:
        stop.set()
        t.join(5)
    assert not t.is_alive()
    assert '"listening"' in capsys.readouterr().out


def test_check_fails_when_docker_is_gone(cfg_env, harness):
    stop = threading.Event()
    t = threading.Thread(
        target=cli.main, args=(["run"],), kwargs={"env": cfg_env, "stop": stop}
    )
    t.start()
    try:
        harness.call(_drop(harness))
        time.sleep(0.3)
        assert cli.main(["check"], env=cfg_env) == 1
    finally:
        stop.set()
        t.join(5)


async def _drop(h) -> None:
    h._uds.close()
    await h._uds.wait_closed()
    os.unlink(h.docker.path)
