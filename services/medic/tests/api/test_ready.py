"""`check --ready`: the Helm readiness probe (C5: not Ready only while the API
can't serve). Ready = Medic's own listener answers the status op with 200, asked
the way the gateway asks (the key from Medic's own key file)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from services.medic.api.ready import check_ready
from services.medic.api.server import StatusBoard, make_server
from services.medic.app.cli import main
from services.medic.tests.api.test_server import CANARY, KEY, _snapshot
from services.medic.tests.fakes import FakeClock


def _env(key_file: Path | None, port: int, **extra: str) -> dict[str, str]:
    env = {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_INSTALL_SHAPE": "helm",
        "VIGIL_MEDIC_API_PORT": str(port),
        **extra,
    }
    if key_file is not None:
        env["VIGIL_MEDIC_API_KEY_FILE"] = str(key_file)
    return env


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "api_key"
    path.write_text(KEY + "\n")
    return path


@pytest.fixture
def serve(key_file):
    servers = []

    def _serve(board: StatusBoard, wall=None):
        clock = FakeClock()
        srv = make_server(
            ("127.0.0.1", 0), key_file=key_file, board=board, wall=wall or clock.wall
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1], clock

    yield _serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _fresh_board(clock: FakeClock) -> StatusBoard:
    board = StatusBoard()
    now = clock.wall()
    board.publish(_snapshot(now), started_at=now - 60, taken_at=now)
    return board


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ready_when_the_status_op_answers(serve, key_file, tmp_path) -> None:
    clock = FakeClock()
    port, _ = serve(_fresh_board(clock), wall=clock.wall)
    ok, reason = check_ready(_env(key_file, port), tmp_path)
    assert ok, reason
    assert reason.startswith("ready")


def test_not_ready_before_the_first_snapshot(serve, key_file, tmp_path) -> None:
    port, _ = serve(StatusBoard())
    ok, reason = check_ready(_env(key_file, port), tmp_path)
    assert not ok
    assert "503" in reason


def test_not_ready_when_nothing_listens(key_file, tmp_path) -> None:
    port = _free_port()
    ok, reason = check_ready(_env(key_file, port), tmp_path)
    assert not ok
    assert "not answering" in reason and str(port) in reason


def test_not_ready_without_a_key_setting_on_helm(tmp_path) -> None:
    ok, reason = check_ready(_env(None, _free_port()), tmp_path)
    assert not ok
    assert "VIGIL_MEDIC_API_KEY_FILE" in reason


def test_not_ready_with_a_key_that_isnt_x2s_shape(tmp_path) -> None:
    bad = tmp_path / "api_key"
    bad.write_text("short")
    ok, reason = check_ready(_env(bad, _free_port()), tmp_path)
    assert not ok
    assert "no usable key" in reason and "short" not in reason


def test_a_bad_api_setting_is_not_ready(key_file, tmp_path) -> None:
    env = _env(key_file, 8470)
    env["VIGIL_MEDIC_API_PORT"] = "99999"
    ok, reason = check_ready(env, tmp_path)
    assert not ok
    assert "99999" not in reason


def test_a_silent_listener_times_out_within_the_budget(key_file, tmp_path) -> None:
    with socket.socket() as srv:  # accepts (backlog) and never answers
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        t0 = time.monotonic()
        ok, reason = check_ready(_env(key_file, port), tmp_path, timeout=0.5)
    assert not ok and "TimeoutError" in reason
    assert time.monotonic() - t0 < 3


def test_the_key_never_reaches_the_reason(serve, tmp_path) -> None:
    canary = tmp_path / "canary_key"
    canary.write_text(CANARY)
    clock = FakeClock()
    port, _ = serve(_fresh_board(clock), wall=clock.wall)  # expects KEY: 401
    ok, reason = check_ready(_env(canary, port), tmp_path)
    assert not ok and "401" in reason
    assert CANARY not in reason


def test_compose_asks_on_the_address_toward_the_gateway(
    serve, key_file, tmp_path
) -> None:
    # Compose binds only the address that routes to the gateway (S9-1), not
    # loopback; `--ready` asks there. A peer of 127.0.0.1 routes via loopback.
    clock = FakeClock()
    port, _ = serve(_fresh_board(clock), wall=clock.wall)
    env = _env(
        key_file,
        port,
        VIGIL_MEDIC_INSTALL_SHAPE="compose",
        VIGIL_MEDIC_API_BIND_PEER="127.0.0.1",
    )
    ok, reason = check_ready(env, tmp_path)
    assert ok, reason


def test_cli_check_ready_exit_codes(serve, key_file, tmp_path, capsys) -> None:
    clock = FakeClock()
    port, _ = serve(_fresh_board(clock), wall=clock.wall)
    env = _env(key_file, port, VIGIL_MEDIC_DATA_DIR=str(tmp_path))
    assert main(["check", "--ready"], env=env) == 0
    assert capsys.readouterr().out.startswith("ready")
    env["VIGIL_MEDIC_API_PORT"] = str(_free_port())
    assert main(["check", "--ready"], env=env) == 1


def test_cli_check_ready_ignores_the_heartbeat(serve, key_file, tmp_path) -> None:
    # Liveness is the heartbeat's job; readiness is only "can the API serve".
    clock = FakeClock()
    port, _ = serve(_fresh_board(clock), wall=clock.wall)
    env = _env(key_file, port, VIGIL_MEDIC_DATA_DIR=str(tmp_path))
    assert main(["check"], env=env) == 1  # no heartbeat in tmp_path
    assert main(["check", "--ready"], env=env) == 0


@pytest.mark.parametrize("argv", [["check", "--bogus"], ["run", "--ready"]])
def test_cli_rejects_other_flags(argv, tmp_path) -> None:
    env = {"VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(argv, env=env) == 2
