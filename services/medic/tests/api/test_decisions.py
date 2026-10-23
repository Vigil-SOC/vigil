"""`GET /v1/decisions` and `GET /v1/decisions/{seq}` against X2's schemas."""

from __future__ import annotations

import json
import logging
import threading

from services.medic.api.decisions import decode_cursor, encode_cursor
from services.medic.api.server import StatusBoard, make_server
from services.medic.store import open_writer
from services.medic.tests.api.schema import errors, record_errors
from services.medic.tests.api.test_server import KEY, _snapshot, assert_problem, call
from services.medic.tests.fakes import FakeClock
from services.medic.tests.store.chains import draft, load, raw, seed_instance

_TYPES = (
    "incident_opened",
    "incident_updated",
    "incident_resolved",
    "feedback",
    "adjudication",
    "anchor",
    "store_reset",
    "pack_event",
)


def _gap(i: int) -> dict:
    minute, second = divmod(i, 60)
    stamp = f"2026-01-01T00:{minute:02d}:{second:02d}Z"
    return {
        "v": 1,
        "at": stamp,
        "type": "gap",
        "body": {"from": "2026-01-01T00:00:00Z", "to": stamp, "reason": "off"},
    }


def _serve(tmp_path):
    key = tmp_path / "api_key"
    key.write_text(KEY + "\n")
    clock = FakeClock()
    board = StatusBoard()
    now = clock.wall()
    board.publish(_snapshot(now), started_at=now - 60, taken_at=now)
    data = tmp_path / "data"
    srv = make_server(
        ("127.0.0.1", 0),
        key_file=key,
        board=board,
        wall=clock.wall,
        data_dir=data,
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, data


def _stop(srv) -> None:
    srv.shutdown()
    srv.server_close()


def _write(data, records) -> None:
    with open_writer(data) as writer:
        for record in records:
            writer.append(record)


def _page(srv, query: str = ""):
    target = "/v1/decisions" + (("?" + query) if query else "")
    status, headers, body = call(srv, target=target, headers={"X-Medic-Key": KEY})
    assert status == 200, body
    assert headers["content-type"] == "application/json"
    doc = json.loads(body)
    assert errors("DecisionPage", doc) == []
    return doc, body


def test_cursor_binds_filters_to_an_uninterpreted_position() -> None:
    token = encode_cursor('{"k":"not-an-incident"}', "next")
    assert token.isascii() and 1 <= len(token) <= 256
    assert decode_cursor(token) == ('{"k":"not-an-incident"}', "next")
    # Same inputs, same token: nothing is signed and nothing is random.
    assert encode_cursor('{"k":"not-an-incident"}', "next") == token
    altered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert decode_cursor(altered) != ('{"k":"not-an-incident"}', "next")
    assert decode_cursor("%%%") is None


def test_an_empty_store_is_an_empty_page(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        _write(data, [])
        doc, _ = _page(srv)
        assert doc == {"items": [], "next_cursor": None, "oldest_seq": None}
        doc = assert_problem(
            *call(srv, target="/v1/decisions/0", headers={"X-Medic-Key": KEY}),
            404,
            "not_found",
        )
        assert "field" not in doc
    finally:
        _stop(srv)


def test_records_come_back_unchanged_and_oldest_seq_ignores_filters(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        chain = load("chain-04-gap.jsonl")
        seed_instance(data)  # the fixture chain names its instance
        _write(data, [draft(rec) for rec in chain])
        doc, body = _page(srv, "limit=100")
        assert doc["oldest_seq"] == 0
        assert [item["seq"] for item in doc["items"]] == [r["seq"] for r in chain]
        for item, rec in zip(doc["items"], chain, strict=True):
            assert item == rec and record_errors(item) == []
            assert json.dumps(item, separators=(",", ":")).encode() in body
        one, _, raw_body = call(
            srv, target="/v1/decisions/0", headers={"X-Medic-Key": KEY}
        )
        assert one == 200 and json.loads(raw_body) == chain[0]
        assert record_errors(json.loads(raw_body)) == []
        # The stored text is what the body is (canonical JSON, ASCII).
        assert raw_body == json.dumps(chain[0], separators=(",", ":")).encode()

        incident = "inc_50eeae0570ffe3e235dd00c0"
        filtered, _ = _page(srv, f"incident_id={incident}&limit=100")
        assert filtered["oldest_seq"] == 0
        assert filtered["items"]
        assert all(
            item["body"]["incident_id"] == incident for item in filtered["items"]
        )
        assert all(item["type"] != "gap" for item in filtered["items"])
        gaps, _ = _page(srv, "type=gap&limit=100")
        assert [item["seq"] for item in gaps["items"]] == [1, 3]
        assert gaps["oldest_seq"] == 0
        # A record with no body.incident_id does not match the filter.
        none, _ = _page(srv, "incident_id=inc_zzzzzzzz&limit=100")
        assert none["items"] == [] and none["next_cursor"] is None
        assert none["oldest_seq"] == 0
    finally:
        _stop(srv)


def test_oldest_seq_is_the_smallest_still_held(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        purged = load("chain-02-after-purge.jsonl")
        seed_instance(data)
        with open_writer(data):
            pass
        conn = raw(data)
        conn.executemany(
            "INSERT INTO records (seq, hash, record) VALUES (?, ?, ?)",
            [
                (rec["seq"], rec["hash"], json.dumps(rec, separators=(",", ":")))
                for rec in purged
            ],
        )
        conn.close()
        doc, _ = _page(srv, "from_seq=0&limit=1")
        assert doc["oldest_seq"] == purged[0]["seq"] == 3
        assert doc["items"][0]["seq"] == 3
        assert decode_cursor(doc["next_cursor"])[1] == "4"
        nxt, _ = _page(srv, f"cursor={doc['next_cursor']}&limit=1")
        assert nxt["items"][0]["seq"] == purged[1]["seq"]
        assert nxt["oldest_seq"] == 3
        missed = assert_problem(
            *call(srv, target="/v1/decisions/0", headers={"X-Medic-Key": KEY}),
            404,
            "not_found",
        )
        assert "field" not in missed
    finally:
        _stop(srv)


def test_from_seq_is_inclusive_and_the_cursor_starts_after_the_page(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        _write(data, [_gap(i) for i in range(6)])
        first, _ = _page(srv, "from_seq=2&limit=2")
        assert [item["seq"] for item in first["items"]] == [2, 3]
        filters, position = decode_cursor(first["next_cursor"])
        assert (filters, position) == ("", "4")
        second, _ = _page(srv, f"cursor={first['next_cursor']}&limit=2")
        assert [item["seq"] for item in second["items"]] == [4, 5]
        assert second["next_cursor"] is None
        # A start past SQLite's range is an empty page, not a 400.
        past, _ = _page(srv, "from_seq=100000000000000000000")
        assert past["items"] == [] and past["next_cursor"] is None
        # The cursor is bound to the filters it was issued with.
        issued, _ = _page(srv, "type=gap&limit=2")
        bad = assert_problem(
            *call(
                srv,
                target=f"/v1/decisions?cursor={issued['next_cursor']}",
                headers={"X-Medic-Key": KEY},
            ),
            400,
            "invalid_cursor",
        )
        assert bad["field"] == "cursor"
        again, _ = _page(srv, f"type=gap&cursor={issued['next_cursor']}&limit=2")
        assert [item["seq"] for item in again["items"]] == [2, 3]
    finally:
        _stop(srv)


def test_pages_each_seq_once_while_a_writer_appends(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    ready, step, landed = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def produce() -> None:
        try:
            with open_writer(data) as writer:
                for i in range(3):
                    writer.append(_gap(i))
                ready.set()
                for i in range(3, 8):
                    assert step.wait(5)
                    step.clear()
                    writer.append(_gap(i))
                    landed.set()
        except BaseException as exc:  # noqa: BLE001 (reported below)
            errors.append(exc)
            ready.set()
            landed.set()

    thread = threading.Thread(target=produce)
    thread.start()
    try:
        assert ready.wait(5)
        first, _ = _page(srv, "limit=2")
        assert [item["seq"] for item in first["items"]] == [0, 1]
        assert first["oldest_seq"] == 0
        seen = [0, 1]
        cursor = first["next_cursor"]
        pages = 1
        while len(seen) < 8:
            # One append lands between every two page reads, so each read races a writer.
            # A null next_cursor means caught up now; the client resumes at from_seq.
            step.set()
            assert landed.wait(5)
            landed.clear()
            if cursor:
                doc, _ = _page(srv, f"cursor={cursor}&limit=2")
            else:
                doc, _ = _page(srv, f"from_seq={seen[-1] + 1}&limit=2")
            seen.extend(item["seq"] for item in doc["items"])
            assert doc["oldest_seq"] == 0
            cursor = doc["next_cursor"]
            pages += 1
        assert pages > 1
        assert seen == list(range(8))
    finally:
        step.set()
        thread.join(5)
        _stop(srv)


def test_limit_defaults_to_50_and_stops_at_100(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        _write(data, [_gap(i) for i in range(60)])
        page, _ = _page(srv)
        assert [item["seq"] for item in page["items"]] == list(range(50))
        assert page["next_cursor"]
        rest, _ = _page(srv, f"cursor={page['next_cursor']}")
        assert [item["seq"] for item in rest["items"]] == list(range(50, 60))
        assert rest["next_cursor"] is None
        wider, _ = _page(srv, "limit=100")
        assert len(wider["items"]) == 60 and wider["next_cursor"] is None
    finally:
        _stop(srv)


def test_query_errors_name_the_parameter_and_never_the_value(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        _write(data, [_gap(0)])
        cases = [
            ("nope=inc_secretvalue", "invalid_parameter", "nope", "inc_secretvalue"),
            ("limit=0", "invalid_parameter", "limit", None),
            ("limit=101", "invalid_parameter", "limit", None),
            ("limit=050", "invalid_parameter", "limit", None),
            ("from_seq=-1", "invalid_parameter", "from_seq", None),
            ("type=not_a_type", "invalid_parameter", "type", "not_a_type"),
            ("incident_id=inc_short", "invalid_parameter", "incident_id", "inc_short"),
            (
                "type=gap&type=gap&from_seq=0&cursor=abc",
                "invalid_parameter",
                "from_seq",
                None,
            ),
            (
                "&".join(f"type={t}" for t in _TYPES) + "&type=gap",
                "invalid_parameter",
                "type",
                None,
            ),
            (
                "cursor=notadecodabletokenZZ",
                "invalid_cursor",
                "cursor",
                "notadecodabletokenZZ",
            ),
            ("cursor=" + "a" * 257, "invalid_cursor", "cursor", None),
            (
                "cursor=" + encode_cursor("", "next"),
                "invalid_cursor",
                "cursor",
                None,
            ),
        ]
        for query, code, field, hidden in cases:
            doc = assert_problem(
                *call(
                    srv,
                    target="/v1/decisions?" + query,
                    headers={"X-Medic-Key": KEY},
                ),
                400,
                code,
            )
            assert doc["field"] == field
            if hidden:
                assert hidden not in json.dumps(doc)
        # Eight types is inside the contract; the value is not what is wrong above.
        ok, _ = _page(srv, "&".join(f"type={t}" for t in _TYPES))
        assert ok["items"] == [] and ok["oldest_seq"] == 0
        # A name that can't be a `field` is still a 400, and the value stays out.
        weird = assert_problem(
            *call(
                srv,
                target="/v1/decisions?%3Cscript%3E=payload99",
                headers={"X-Medic-Key": KEY},
            ),
            400,
            "invalid_parameter",
        )
        assert "field" not in weird and "payload99" not in json.dumps(weird)
    finally:
        _stop(srv)


def test_get_decision_errors_and_the_wrong_method(tmp_path) -> None:
    srv, data = _serve(tmp_path)
    try:
        _write(data, [_gap(0)])
        for target, field, hidden in (
            ("/v1/decisions/nope", "seq", "nope"),
            ("/v1/decisions/01", "seq", None),
            ("/v1/decisions/-1", "seq", None),
            ("/v1/decisions/0?limit=1", "limit", None),
        ):
            doc = assert_problem(
                *call(srv, target=target, headers={"X-Medic-Key": KEY}),
                400,
                "invalid_parameter",
            )
            assert doc["field"] == field
            if hidden:
                assert hidden not in json.dumps(doc)
        # A well-formed seq past SQLite's range is absent, not malformed.
        for target in (
            "/v1/decisions/9223372036854775808",
            "/v1/decisions/" + "9" * 40,
        ):
            assert_problem(
                *call(srv, target=target, headers={"X-Medic-Key": KEY}),
                404,
                "not_found",
            )
        for target in ("/v1/decisions", "/v1/decisions/0"):
            assert_problem(
                *call(srv, method="POST", target=target, headers={"X-Medic-Key": KEY}),
                405,
                "method_not_allowed",
            )
        assert_problem(
            *call(srv, target="/v1/decisions/0"),
            401,
            "unauthorized",
        )
        assert_problem(
            *call(srv, target="/v1/export", headers={"X-Medic-Key": KEY}),
            404,
            "not_found",
        )
        # Status is unchanged by the decision routes.
        assert call(srv, headers={"X-Medic-Key": KEY})[0] == 200
    finally:
        _stop(srv)


def test_a_missing_or_unreadable_store_is_busy(tmp_path, caplog) -> None:
    srv, data = _serve(tmp_path)
    try:
        with caplog.at_level(logging.DEBUG):
            for target in ("/v1/decisions", "/v1/decisions/1"):
                doc = assert_problem(
                    *call(srv, target=target, headers={"X-Medic-Key": KEY}),
                    503,
                    "busy",
                )
                assert doc["retry_after_s"] >= 1
                assert "field" not in doc
        data.mkdir()
        (data / "medic.db").write_bytes(b"this is not a database")
        assert_problem(
            *call(srv, target="/v1/decisions", headers={"X-Medic-Key": KEY}),
            503,
            "busy",
        )
        assert "medic.db" not in caplog.text
    finally:
        _stop(srv)


def test_the_widest_legal_filter_still_returns_a_cursor(tmp_path) -> None:
    """incident_id at its max plus eight types is a legal page, and the cursor
    still has to match `Cursor` (256 characters)."""
    srv, data = _serve(tmp_path)
    try:
        incident = "inc_" + "a" * 32
        seed_instance(data)
        with open_writer(data):
            pass
        conn = raw(data)
        for seq in (0, 1):
            record = {
                "seq": seq,
                "type": "incident_resolved",
                "body": {"incident_id": incident},
            }
            conn.execute(
                "INSERT INTO records (seq, hash, record) VALUES (?, ?, ?)",
                (seq, f"h{seq}", json.dumps(record, separators=(",", ":"))),
            )
        conn.close()
        types = "&".join(f"type={name}" for name in _TYPES)
        query = f"incident_id={incident}&limit=1&{types}"
        status, _, body = call(
            srv, target="/v1/decisions?" + query, headers={"X-Medic-Key": KEY}
        )
        assert status == 200, body
        doc = json.loads(body)
        token = doc["next_cursor"]
        assert isinstance(token, str) and len(token) <= 256
        bound, position = decode_cursor(token)
        assert position == "1"
        assert incident in bound and "incident_resolved" in bound
        status, _, body = call(
            srv,
            target=f"/v1/decisions?cursor={token}&limit=1&incident_id={incident}&{types}",
            headers={"X-Medic-Key": KEY},
        )
        assert status == 200, body
        assert [item["seq"] for item in json.loads(body)["items"]] == [1]
    finally:
        _stop(srv)


def test_no_data_dir_is_busy_and_does_not_route_export(tmp_path) -> None:
    key = tmp_path / "api_key"
    key.write_text(KEY + "\n")
    clock = FakeClock()
    board = StatusBoard()
    now = clock.wall()
    board.publish(_snapshot(now), started_at=now - 60, taken_at=now)
    srv = make_server(("127.0.0.1", 0), key_file=key, board=board, wall=clock.wall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert_problem(
            *call(srv, target="/v1/decisions", headers={"X-Medic-Key": KEY}),
            503,
            "busy",
        )
        assert_problem(
            *call(srv, target="/v1/export", headers={"X-Medic-Key": KEY}),
            404,
            "not_found",
        )
    finally:
        _stop(srv)
