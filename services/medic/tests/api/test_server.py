"""The listener: `X-Medic-Key` first, then exactly one route (X2; the other 8 are G3's)."""

from __future__ import annotations

import http.client
import itertools
import json
import logging
import socket
import threading
import time

import pytest

from services.medic.api.history import History
from services.medic.api.server import (
    SNAPSHOT_MAX_AGE_S,
    Api,
    StatusBoard,
    make_server,
)
from services.medic.api.status import build_status
from services.medic.store.writer import GIB, RESERVE_BYTES, StoreUsage
from services.medic.tests.api.schema import errors
from services.medic.tests.fakes import FakeClock

KEY = "k" * 21 + "_-" + "Z9" * 10  # 43 chars, X2's shape
CANARY = "CANARYkeyCANARYkeyCANARYkeyCANARYkeyCANARY1"  # also 43 chars
assert len(KEY) == len(CANARY) == 43


def _snapshot(now: float) -> dict:
    return build_status(
        instance_id="mi_3f9a2c1b0d4e5f60",
        now=now,
        started_at=now - 60,
        heartbeat_at=now,
        cycle=4,
        history=History(restarts=(), last_exit=None),
        scheduler={"sensors": 1, "off": {}, "blind": []},
        usage=StoreUsage(0, GIB, RESERVE_BYTES, 10 * GIB, "ok"),
        decisions_lost=0,
        chain={"head": None, "oldest_at": None, "newest_at": None},
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "api_key"
    path.write_text(KEY + "\n")
    return path


@pytest.fixture
def board(clock) -> StatusBoard:
    b = StatusBoard()
    b.publish(
        _snapshot(clock.wall()), started_at=clock.wall() - 60, taken_at=clock.wall()
    )
    return b


@pytest.fixture
def server(key_file, board, clock):
    srv = make_server(("127.0.0.1", 0), key_file=key_file, board=board, wall=clock.wall)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(server, method="GET", target="/v1/status", headers=None, raw=None):
    host, port = server.server_address[:2]
    if raw is not None:
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall(raw)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), {}, body
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, target, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def assert_problem(status, headers, body, want_status, want_code) -> dict:
    assert status == want_status
    assert headers["content-type"] == "application/problem+json"
    doc = json.loads(body)
    assert errors("Error", doc) == []
    assert (doc["status"], doc["code"]) == (want_status, want_code)
    assert doc["type"] == f"urn:medic:error:{want_code}"
    return doc


def test_status_with_the_key_is_schema_valid(server) -> None:
    status, headers, body = call(server, headers={"X-Medic-Key": KEY})
    assert status == 200
    assert headers["content-type"] == "application/json"
    doc = json.loads(body)
    assert errors("Status", doc) == []
    # X2: every response carries these three.
    assert headers["cache-control"] == "no-store"
    assert headers["x-medic-api-version"] == "1.0"
    assert headers["x-request-id"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Medic-Key": CANARY},
        {"X-Medic-Key": KEY[:-1]},
        {"X-Medic-Key": KEY + "x"},
    ],
)
def test_missing_or_wrong_key_is_401_problem_json(server, headers) -> None:
    assert_problem(*call(server, headers=headers), 401, "unauthorized")


def test_401_comes_before_routing(server) -> None:
    # X2: no key, no hint that a path exists.
    assert_problem(*call(server, target="/v1/nope"), 401, "unauthorized")
    assert_problem(
        *call(server, method="POST", target="/v1/status"), 401, "unauthorized"
    )


@pytest.mark.parametrize(
    "target",
    [
        "/v1/incidents",  # G3's, not served yet
        "/v1/decisions/1",
        "/v1/status/",
        "/v1/status?x=1",
        "/v2/status",
        "//v1/status",
        "/",
    ],
)
def test_any_other_path_is_404(server, target) -> None:
    assert_problem(
        *call(server, target=target, headers={"X-Medic-Key": KEY}), 404, "not_found"
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "HEAD", "get"])
def test_other_methods_on_status_are_405(server, method) -> None:
    status, headers, body = call(server, method=method, headers={"X-Medic-Key": KEY})
    assert status == 405
    if method != "HEAD":  # no body on a HEAD reply
        assert_problem(status, headers, body, 405, "method_not_allowed")


def test_request_id_is_echoed_only_when_well_formed(server) -> None:
    ok = {"X-Medic-Key": KEY, "X-Request-Id": "abc-123"}
    assert call(server, headers=ok)[1]["x-request-id"] == "abc-123"
    bad = {"X-Medic-Key": KEY, "X-Request-Id": "<script>"}
    rid = call(server, headers=bad)[1]["x-request-id"]
    assert rid != "<script>" and len(rid) <= 64
    doc = assert_problem(
        *call(server, headers={"X-Request-Id": "r-1"}), 401, "unauthorized"
    )
    assert doc["request_id"] == "r-1"


def test_a_malformed_request_gets_problem_json_without_echo(server) -> None:
    status, _, body = call(server, raw=b"GET /v1/status HTTP/9.9<b>\r\n\r\n")
    assert status == 400
    assert b"<b>" not in body
    assert json.loads(body)["code"] == "invalid_parameter"


def test_no_snapshot_yet_is_503_busy(key_file, clock) -> None:
    srv = make_server(
        ("127.0.0.1", 0), key_file=key_file, board=StatusBoard(), wall=clock.wall
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        doc = assert_problem(*call(srv, headers={"X-Medic-Key": KEY}), 503, "busy")
        assert doc["retry_after_s"] >= 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_snapshot_age_limit(server, clock) -> None:
    # X2: "a snapshot no older than 15 s", plus a cycle's own run time.
    assert SNAPSHOT_MAX_AGE_S <= 15 + 5
    clock.advance(SNAPSHOT_MAX_AGE_S)
    status, _, body = call(server, headers={"X-Medic-Key": KEY})
    assert status == 200
    doc = json.loads(body)
    assert doc["now"] > doc["heartbeat_at"]  # `now` is the serve time
    clock.advance(1)  # the loop stopped publishing: stale data is never served
    assert_problem(*call(server, headers={"X-Medic-Key": KEY}), 503, "busy")


def test_the_key_file_is_reread_so_a_new_key_takes_over(server, key_file) -> None:
    key_file.write_text(CANARY)
    assert call(server, headers={"X-Medic-Key": CANARY})[0] == 200
    assert call(server, headers={"X-Medic-Key": KEY})[0] == 401


@pytest.mark.parametrize("content", ["", "short", KEY + KEY, "k" * 42 + "!"])
def test_a_key_file_not_in_x2s_shape_accepts_nothing(server, key_file, content) -> None:
    key_file.write_text(content)
    for given in (content, KEY, ""):
        status = call(server, headers={"X-Medic-Key": given} if given else {})[0]
        assert status == 401


def test_a_missing_key_file_accepts_nothing(server, key_file) -> None:
    key_file.unlink()
    assert call(server, headers={"X-Medic-Key": KEY})[0] == 401


def test_no_secret_in_logs(server, key_file, caplog, capsys) -> None:
    key_file.write_text(CANARY)
    with caplog.at_level(logging.DEBUG):
        for headers in ({"X-Medic-Key": CANARY}, {"X-Medic-Key": CANARY[::-1]}):
            for target in ("/v1/status", "/v1/" + CANARY):
                call(server, target=target, headers=headers)
        call(
            server,
            raw=b"GET /"
            + CANARY.encode()
            + b" HTTP/1.1\r\nX-Medic-Key: "
            + CANARY.encode()
            + b"\r\n\r\n",
        )
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert CANARY not in text and CANARY[::-1] not in text


# -- the supervisor: never a reason for Medic to die (C5) ---------------------------


def _api(key_file, board, clock, port, **kw) -> Api:
    return Api(
        ("127.0.0.1", port),
        key_file=key_file,
        board=board,
        wall=clock.wall,
        monotonic=clock.monotonic,
        **kw,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_api_starts_and_stops(key_file, board, clock) -> None:
    api = _api(key_file, board, clock, _free_port())
    try:
        api.ensure()
        assert api.running
        status = call(api.server, headers={"X-Medic-Key": KEY})[0]
        assert status == 200
    finally:
        api.stop()
    assert not api.running


def test_a_taken_port_is_logged_once_and_retried(
    key_file, board, clock, caplog
) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    api = _api(key_file, board, clock, port)
    try:
        with caplog.at_level(logging.WARNING, logger="services.medic"):
            api.ensure()
            api.ensure()
            clock.advance(61)
            api.ensure()
        assert not api.running
        assert caplog.text.count("can't listen") == 1
        holder.close()
        clock.advance(61)
        api.ensure()
        assert api.running
    finally:
        holder.close()
        api.stop()


def test_no_key_file_means_no_listener_and_says_why(
    tmp_path, board, clock, caplog
) -> None:
    missing = tmp_path / "nope"
    api = _api(missing, board, clock, _free_port())
    with caplog.at_level(logging.WARNING, logger="services.medic"):
        api.ensure()
        clock.advance(61)
        api.ensure()
    assert not api.running
    assert caplog.text.count("Medic's API is off") == 1
    assert str(missing) in caplog.text
    missing.write_text(KEY)
    clock.advance(61)
    try:
        api.ensure()
        assert api.running
    finally:
        api.stop()


def test_no_key_file_setting_says_which_variable(board, clock, caplog) -> None:
    api = _api(None, board, clock, _free_port())
    with caplog.at_level(logging.WARNING, logger="services.medic"):
        api.ensure()
    assert not api.running
    assert "VIGIL_MEDIC_API_KEY_FILE" in caplog.text


def test_bind_peer_narrows_the_listener_to_the_address_toward_it(
    key_file, board, clock, caplog
) -> None:
    # Compose: only the address on the gateway's network (medic-private), never
    # medic-net, where the agents could reach it (K1 §6 G3).
    api = _api(key_file, board, clock, _free_port(), bind_peer="localhost")
    api.bind = ("0.0.0.0", api.bind[1])  # what config gives a container shape
    try:
        with caplog.at_level(logging.INFO, logger="services.medic"):
            api.ensure()
        assert api.running
        assert api.server.server_address[0] == "127.0.0.1"
        assert "listening on 127.0.0.1:" in caplog.text
    finally:
        api.stop()


def test_an_unresolvable_peer_waits_and_retries(key_file, board, clock, caplog) -> None:
    api = _api(key_file, board, clock, _free_port(), bind_peer="medic-test.invalid")
    with caplog.at_level(logging.WARNING, logger="services.medic"):
        api.ensure()
        clock.advance(61)
        api.ensure()
    assert not api.running
    assert caplog.text.count("isn't listening yet") == 1
    assert "medic-test.invalid" in caplog.text


def test_first_retries_are_quick_then_once_a_minute(tmp_path, board, clock) -> None:
    api = _api(tmp_path / "nope", board, clock, _free_port())
    tries = []
    real = api._try_start
    api._try_start = lambda: tries.append(clock.monotonic()) or real()
    for _ in range(400):
        api.ensure()
        clock.advance(1)
    gaps = [b - a for a, b in itertools.pairwise(tries)]
    assert gaps[:4] == [5, 10, 20, 40] and set(gaps[4:]) == {60}


def test_more_than_four_in_flight_is_503_busy(key_file, board, clock) -> None:
    """X2 Busy: over 4 requests in flight answer 503 busy with Retry-After, never
    a dropped connection (the backend would read that as refused)."""
    import services.medic.api.server as srv_mod

    gate = threading.Event()
    real_read = board.read

    def slow_read():
        gate.wait(5)
        return real_read()

    board.read = slow_read
    srv = make_server(("127.0.0.1", 0), key_file=key_file, board=board, wall=clock.wall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    results = []
    try:
        threads = [
            threading.Thread(
                target=lambda: results.append(call(srv, headers={"X-Medic-Key": KEY}))
            )
            for _ in range(srv_mod.MAX_IN_FLIGHT)
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5
        while srv.in_flight._value > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        status, headers, body = call(srv, headers={"X-Medic-Key": KEY})
        doc = assert_problem(status, headers, body, 503, "busy")
        assert headers["retry-after"] == str(doc["retry_after_s"])
        gate.set()
        for t in threads:
            t.join(5)
        assert [r[0] for r in results] == [200] * srv_mod.MAX_IN_FLIGHT
    finally:
        gate.set()
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("peer", ["a" * 64, "x" * 63 + "." + "y" * 64])
def test_a_peer_that_cannot_be_encoded_never_raises(
    key_file, board, clock, peer, caplog
):
    api = _api(key_file, board, clock, _free_port(), bind_peer=peer)
    with caplog.at_level(logging.WARNING, logger="services.medic"):
        api.ensure()  # UnicodeError from the IDNA codec must not escape
    assert not api.running
    assert "isn't listening yet" in caplog.text


def test_the_listener_tick_is_the_loops() -> None:
    from services.medic.api import server
    from services.medic.app.wiring import TICK_S

    assert server.TICK_S == TICK_S
