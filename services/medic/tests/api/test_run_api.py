"""The API inside `run`: a snapshot per cycle, and never a reason to fail `check` (C5)."""

from __future__ import annotations

import http.client
import json
import logging
import socket

from services.medic.api.history import history_path
from services.medic.app import config
from services.medic.app.cli import main
from services.medic.app.heartbeat import check_heartbeat
from services.medic.app.wiring import TICK_S
from services.medic.tests.api.schema import errors
from services.medic.tests.fakes import FakeClock

KEY = "A" * 40 + "_-9"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, key: str = KEY) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/v1/status", headers={"X-Medic-Key": key})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def _env(tmp_path, port: int) -> dict[str, str]:
    key_file = tmp_path / "secrets" / "api_key"
    key_file.parent.mkdir()
    key_file.write_text(KEY)
    return {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path / "data"),
        "VIGIL_MEDIC_API_PORT": str(port),
        "VIGIL_MEDIC_API_KEY_FILE": str(key_file),
        # Unreachable on purpose: a fast, deterministic "can't see".
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
    }


def test_status_is_served_while_medic_runs(tmp_path) -> None:
    clock, port = FakeClock(), _free_port()
    seen: list[tuple[int, dict, dict]] = []

    async def sleep_and_look(seconds: float) -> None:
        # Right after a cycle: the snapshot it just published.
        fresh = _get(port)
        await clock.sleep(seconds)
        # At the end of the wait, just before the next cycle: the oldest it gets.
        seen.append((*fresh, _get(port)[1]))

    env = _env(tmp_path, port)
    assert main(["run"], env=env, clock=clock, sleep=sleep_and_look, max_cycles=3) == 0

    assert [s for s, _, _ in seen] == [200, 200, 200]
    for _, fresh, oldest in seen:
        assert errors("Status", fresh) == [] and errors("Status", oldest) == []
        assert fresh["now"] == fresh["heartbeat_at"]
        assert _age(oldest) <= TICK_S  # never older than one tick between cycles
    assert [f["cycle"] for _, f, _ in seen] == [1, 2, 3]
    first = seen[0][1]
    assert first["instance_id"].startswith("mi_")
    assert first["state"] == "starting" and first["sensors"]["off"] == 0
    # The listener goes with the loop: nothing answers after `run` returns.
    try:
        _get(port)
        raise AssertionError("still listening after run returned")
    except OSError:
        pass


def _age(doc: dict) -> int:
    from datetime import datetime

    parse = datetime.fromisoformat
    return int((parse(doc["now"]) - parse(doc["heartbeat_at"])).total_seconds())


def test_restart_counts_and_last_exit_come_through(tmp_path) -> None:
    clock, port = FakeClock(), _free_port()
    env = _env(tmp_path, port)
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=1) == 0
    clock.advance(30)
    seen = []

    async def look(seconds: float) -> None:
        seen.append(_get(port)[1])
        await clock.sleep(seconds)

    assert main(["run"], env=env, clock=clock, sleep=look, max_cycles=1) == 0
    assert seen[0]["restarts_24h"] == 1
    assert seen[0]["last_exit"]["reason"] == "clean"
    assert history_path(tmp_path / "data").exists()


def test_a_dead_api_never_fails_check(tmp_path, caplog) -> None:
    clock = FakeClock()
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    env = _env(tmp_path, holder.getsockname()[1])
    healthy = []

    async def sleep_and_check(seconds: float) -> None:
        healthy.append(check_heartbeat(tmp_path / "data", now=clock.wall())[0])
        await clock.sleep(seconds)

    try:
        with caplog.at_level(logging.WARNING, logger="services.medic"):
            code = main(
                ["run"], env=env, clock=clock, sleep=sleep_and_check, max_cycles=3
            )
    finally:
        holder.close()
    assert code == 0 and healthy == [True, True, True]
    assert caplog.text.count("can't listen") == 1


def test_no_key_runs_without_the_api(tmp_path, caplog) -> None:
    clock = FakeClock()
    env = _env(tmp_path, _free_port())
    del env["VIGIL_MEDIC_API_KEY_FILE"]  # host-native default: <data>/run/api_key
    healthy = []

    async def sleep_and_check(seconds: float) -> None:
        healthy.append(check_heartbeat(tmp_path / "data", now=clock.wall())[0])
        await clock.sleep(seconds)

    with caplog.at_level(logging.WARNING, logger="services.medic"):
        assert (
            main(["run"], env=env, clock=clock, sleep=sleep_and_check, max_cycles=2)
            == 0
        )
    assert healthy == [True, True]
    assert "Medic's API is off" in caplog.text
    assert str(tmp_path / "data" / "run" / "api_key") in caplog.text


# -- where it listens (config) ------------------------------------------------------


def test_bind_is_loopback_unless_the_shape_is_explicitly_a_container() -> None:
    port = config.API_PORT_DEFAULT
    assert port == 8470
    assert config.api_bind({}) == ("127.0.0.1", port)
    for shape in ("start_sh", " start_sh "):
        assert config.api_bind({config.SHAPE_VAR: shape}) == ("127.0.0.1", port)
    # Inside its container or pod: Compose's network / Helm's NetworkPolicy fence it.
    for shape in ("compose", "helm"):
        assert config.api_bind({config.SHAPE_VAR: shape}) == ("0.0.0.0", port)
    assert config.api_bind({config.API_PORT_VAR: "9000"})[1] == 9000


def test_bad_port_is_a_config_error_without_the_value() -> None:
    import pytest

    for value in ("0", "70000", "x", "-1", "80 "):
        with pytest.raises(config.ConfigError) as err:
            config.api_bind({config.API_PORT_VAR: value + "SECRET"})
        assert "SECRET" not in str(err.value)


def test_key_file_defaults_per_shape(tmp_path) -> None:
    from pathlib import Path

    assert config.api_key_file({}, tmp_path) == tmp_path / "run" / "api_key"
    start_sh = {config.SHAPE_VAR: "start_sh"}
    assert config.api_key_file(start_sh, tmp_path) == tmp_path / "run" / "api_key"
    compose = {config.SHAPE_VAR: "compose"}
    assert config.api_key_file(compose, tmp_path) == Path("/run/secrets/medic_api_key")
    # Helm: the chart names it (S7b); no guess.
    assert config.api_key_file({config.SHAPE_VAR: "helm"}, tmp_path) is None
    named = {config.API_KEY_FILE_VAR: "/x/key", config.SHAPE_VAR: "helm"}
    assert config.api_key_file(named, tmp_path) == Path("/x/key")


def test_bind_peer_per_shape() -> None:
    import pytest

    assert config.api_bind_peer({config.SHAPE_VAR: "compose"}) == "medic-gateway-out"
    for env in ({}, {config.SHAPE_VAR: "helm"}, {config.SHAPE_VAR: "start_sh"}):
        assert config.api_bind_peer(env) is None
    named = {config.SHAPE_VAR: "helm", config.API_BIND_PEER_VAR: "gw.example"}
    assert config.api_bind_peer(named) == "gw.example"
    with pytest.raises(config.ConfigError) as err:
        config.api_bind_peer({config.API_BIND_PEER_VAR: "a b/SECRET"})
    assert "SECRET" not in str(err.value)


def test_a_bad_api_setting_leaves_the_api_off_not_medic(tmp_path, caplog) -> None:
    clock = FakeClock()
    env = _env(tmp_path, _free_port())
    env[config.API_PORT_VAR] = "99999"
    healthy = []

    async def sleep_and_check(seconds: float) -> None:
        healthy.append(check_heartbeat(tmp_path / "data", now=clock.wall())[0])
        await clock.sleep(seconds)

    with caplog.at_level(logging.ERROR, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=sleep_and_check, max_cycles=2)
    assert code == 0 and healthy == [True, True]
    assert "Medic's API is off" in caplog.text and "99999" not in caplog.text


def test_an_api_that_raises_never_stops_the_loop(tmp_path, monkeypatch, caplog) -> None:
    from services.medic.api import server

    def boom(self):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(server.Api, "_try_start", boom)
    clock = FakeClock()
    env = _env(tmp_path, _free_port())
    with caplog.at_level(logging.ERROR, logger="services.medic"):
        code = main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=3)
    assert code == 0
    assert caplog.text.count("Medic's API failed") == 1


def test_heartbeat_at_is_the_last_beat_written(tmp_path, monkeypatch) -> None:
    from services.medic.app import cli

    clock, port = FakeClock(), _free_port()
    seen = []
    real = cli.write_heartbeat

    def failing_after_start(data_dir, **kw):
        if kw.get("cycle", 0) >= 2 and kw.get("state", "running") == "running":
            raise OSError("disk full")
        return real(data_dir, **kw)

    monkeypatch.setattr(cli, "write_heartbeat", failing_after_start)

    async def look(seconds: float) -> None:
        seen.append(_get(port)[1])
        await clock.sleep(seconds)

    env = _env(tmp_path, port)
    assert main(["run"], env=env, clock=clock, sleep=look, max_cycles=3) == 0
    beats = [d["heartbeat_at"] for d in seen]
    # Cycle 1's beat was the last one written; cycles 2 and 3 failed to write.
    assert beats[1] == beats[0] and beats[2] == beats[0]
    assert seen[2]["now"] > beats[0]
