"""`python -m services.medic_gateway run | check`: configuration, refusals, health check."""

from __future__ import annotations

import socket
import threading

import pytest

from services.medic_gateway import cli
from services.medic_gateway.tests.conftest import PASSWORD


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cfg_env(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    return {
        "VIGIL_MEDIC_GATEWAY_BACKEND": "127.0.0.1:1",
        "VIGIL_MEDIC_GATEWAY_MEDIC": "127.0.0.1:2",
        "VIGIL_MEDIC_GATEWAY_OUT_BIND": f"localhost:{free_port()}",
        "VIGIL_MEDIC_GATEWAY_IN_BIND": f"localhost:{free_port()}",
        "VIGIL_MEDIC_GATEWAY_VIEWER_USER": "medic-viewer",
        "VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD_FILE": str(pw),
    }


@pytest.mark.parametrize(
    "name",
    [
        "AGENT_INTERNAL_TOKEN",
        "VIGIL_TOOLS_TOKEN",
        "DAEMON_WEBHOOK_TOKEN",
        "VIGIL_MEDIC_API_KEY",
        "VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD",
    ],
)
def test_refuses_a_credential_in_its_environment(cfg_env, capsys, name):
    cfg_env[name] = "s3cr3t-value"
    assert cli.main(["run"], env=cfg_env) == 2
    err = capsys.readouterr().err
    assert name in err and "s3cr3t-value" not in err


@pytest.mark.parametrize(
    "change",
    [
        {"VIGIL_MEDIC_GATEWAY_BACKEND": None},
        {"VIGIL_MEDIC_GATEWAY_BACKEND": "backend"},
        {"VIGIL_MEDIC_GATEWAY_BACKEND": "backend:http"},
        {"VIGIL_MEDIC_GATEWAY_VIEWER_USER": ""},
        {"VIGIL_CONTEXT_PATH": "/vigil/"},
        {"VIGIL_CONTEXT_PATH": "/a/../b"},
        {"VIGIL_CONTEXT_PATH": "vigil"},
    ],
)
def test_bad_configuration_exits_2(cfg_env, capsys, change):
    for k, v in change.items():
        if v is None:
            cfg_env.pop(k)
        else:
            cfg_env[k] = v
    assert cli.main(["run"], env=cfg_env) == 2
    assert "medic-gateway:" in capsys.readouterr().err


def test_context_path_is_accepted(cfg_env):
    cfg_env["VIGIL_CONTEXT_PATH"] = "/vigil"
    assert cli.load_config(cfg_env).context_path == "/vigil"


def test_password_file_defaults_to_the_secret_mount(cfg_env):
    cfg_env.pop("VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD_FILE")
    assert (
        str(cli.load_config(cfg_env).password_file)
        == "/run/secrets/medic_viewer_password"
    )


def test_usage(capsys):
    assert cli.main([], env={}) == 2
    assert cli.main(["serve"], env={}) == 2


def test_run_then_check(cfg_env):
    stop = threading.Event()
    t = threading.Thread(
        target=cli.main, args=(["run"],), kwargs={"env": cfg_env, "stop": stop}
    )
    t.start()
    try:
        for _ in range(50):
            if cli.main(["check"], env=cfg_env) == 0:
                break
            stop.wait(0.05)
        else:
            pytest.fail("check never went green")
    finally:
        stop.set()
        t.join(5)
    assert not t.is_alive()
    assert cli.main(["check"], env=cfg_env) == 1, "nothing is listening any more"


def test_listeners_bind_to_the_named_address_only(cfg_env):
    servers = cli.build(cli.load_config(cfg_env))
    try:
        assert [s.server_address[0] for s in servers] == ["127.0.0.1", "127.0.0.1"]
    finally:
        for s in servers:
            s.server_close()
