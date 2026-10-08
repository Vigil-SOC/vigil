"""S9: the backend's poll (V2) against Medic's real `/v1/status` listener.

Medic's side builds and serves the body; the backend's own `fetch_status` and
`typed_snapshot` read it. On host-native this is the whole path (loopback, no
gateway); on Compose the gateway relays the same bytes. If the two drift, this
fails before any live stack does.
"""

from __future__ import annotations

import socket
import threading

import pytest

from core.platform.medic_last_seen import fetch_status, typed_snapshot

medic_server = pytest.importorskip("services.medic.api.server")
from services.medic.api.history import History  # noqa: E402
from services.medic.api.status import build_status  # noqa: E402
from services.medic.store.writer import GIB, RESERVE_BYTES, StoreUsage  # noqa: E402

KEY = "b" * 40 + "-_0"
T0 = 1_800_000_000.0


def _snapshot(**over) -> dict:
    args = {
        "instance_id": "mi_0123456789abcdef",
        "now": T0,
        "started_at": T0 - 4000,
        "heartbeat_at": T0,
        "cycle": 266,
        "history": History(restarts=(T0 - 60,), last_exit=None),
        "scheduler": {"sensors": 2, "off": {}, "blind": []},
        "usage": StoreUsage(10_000, GIB, RESERVE_BYTES, 50 * GIB, "ok"),
        "decisions_lost": 0,
        "chain": {"head": None, "oldest_at": None, "newest_at": None},
    }
    args.update(over)
    return build_status(**args)


VARIANTS = {
    "running": {},
    "starting": {"started_at": T0 - 10},
    "blind": {"scheduler": {"sensors": 2, "off": {}, "blind": ["a", "b"]}},
    "degraded_store": {
        "usage": StoreUsage(GIB + 1, GIB, RESERVE_BYTES, 1, "over_cap"),
        "decisions_lost": 3,
        "chain": {
            "head": {"seq": 41, "hash": "c" * 64},
            "oldest_at": "2027-01-14T00:00:00Z",
            "newest_at": "2027-01-15T07:59:45Z",
        },
    },
    "crash_looping": {
        "history": History(
            restarts=tuple(T0 - i for i in range(1, 8)),
            last_exit={"reason": "watchdog_stall", "at": "2027-01-15T07:59:00Z"},
        )
    },
}


@pytest.fixture
def medic(tmp_path):
    key_file = tmp_path / "api_key"
    key_file.write_text(KEY)
    board = medic_server.StatusBoard()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = medic_server.make_server(
        ("127.0.0.1", port), key_file=key_file, board=board, wall=lambda: T0
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield board, f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_backend_reads_every_medic_state_as_running(medic, name) -> None:
    board, url = medic
    snap = _snapshot(**VARIANTS[name])
    started = VARIANTS[name].get("started_at", T0 - 4000)
    board.publish(snap, started_at=started, taken_at=T0)
    outcome = fetch_status(url, KEY)
    assert outcome.failure is None, outcome
    assert outcome.snapshot == typed_snapshot(snap)
    assert outcome.snapshot["state"] == snap["state"]


def test_backend_maps_medics_refusals(medic) -> None:
    board, url = medic
    assert fetch_status(url, "W" * 43).failure.value == "401"
    # No snapshot yet: Medic says busy (503), the backend counts a 5xx.
    assert fetch_status(url, KEY).failure.value == "5xx"
    board.publish(_snapshot(), started_at=T0 - 4000, taken_at=T0)
    assert fetch_status(url, KEY).failure is None
