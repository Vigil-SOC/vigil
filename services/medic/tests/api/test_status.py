"""`GET /v1/status`'s body (X2 `Status`), built from what the main loop knows (C5 §5.4)."""

from __future__ import annotations

import pytest

from services.medic.api.history import History
from services.medic.api.status import (
    API_VERSION,
    build_status,
    medic_state,
    read_chain,
    serve_view,
)
from services.medic.store import open_writer
from services.medic.store.writer import GIB, MIB, RESERVE_BYTES, StoreUsage
from services.medic.tests.api.schema import SPEC, errors

T0 = 1_800_000_000.0


def _usage(used: int = 10 * MIB, cap: int = GIB, state: str = "ok") -> StoreUsage:
    return StoreUsage(
        used_bytes=used,
        cap_bytes=cap,
        reserve_bytes=RESERVE_BYTES,
        free_bytes=40 * GIB,
        state=state,
    )


def _scheduler(sensors: int = 1, off=None, blind=()) -> dict:
    return {
        "sensors": sensors,
        "off": dict(off or {}),
        "blind": sorted(blind),
        "dropped": 0,
        "redactor_failures": 0,
        "vigil_dev_mode": False,
    }


def _status(**over) -> dict:
    args = {
        "instance_id": "mi_3f9a2c1b0d4e5f60",
        "now": T0 + 600,
        "started_at": T0,
        "heartbeat_at": T0 + 600,
        "cycle": 40,
        "history": History(restarts=(), last_exit=None),
        "scheduler": _scheduler(),
        "usage": _usage(),
        "decisions_lost": 0,
        "chain": {"head": None, "oldest_at": None, "newest_at": None},
    }
    args.update(over)
    return build_status(**args)


def test_a_fresh_medic_is_schema_valid_and_running() -> None:
    body = _status()
    assert errors("Status", body) == []
    assert body["api_version"] == API_VERSION
    assert body["state"] == "running"
    assert body["now"] == "2027-01-15T08:10:00Z"
    assert body["uptime_s"] == 600
    assert body["sensors"] == {"reporting": 1, "cant_see": 0, "off": 0}
    # Until F6 loads packs, Medic runs the unsigned dev rules (UX-9).
    assert body["pack"] is None and body["dev_mode"] is True
    assert body["chain_head"] is None and body["restarts_24h"] == 0


def test_every_field_is_filled_from_its_source() -> None:
    body = _status(
        history=History(
            restarts=(T0 - 3600, T0 - 7200),
            last_exit={"reason": "watchdog_stall", "at": "2027-01-15T07:00:00Z"},
        ),
        scheduler=_scheduler(sensors=4, off={"a": "vigil_dev_mode"}, blind=["b"]),
        usage=_usage(used=900 * MIB, state="near_cap"),
        decisions_lost=2,
        chain={
            "head": {"seq": 7, "hash": "a" * 64},
            "oldest_at": "2027-01-14T00:00:00Z",
            "newest_at": "2027-01-15T08:09:45Z",
        },
    )
    assert errors("Status", body) == []
    assert body["restarts_24h"] == 2
    assert body["last_exit"] == {
        "reason": "watchdog_stall",
        "at": "2027-01-15T07:00:00Z",
    }
    assert body["sensors"] == {"reporting": 2, "cant_see": 1, "off": 1}
    assert body["chain_head"] == {"seq": 7, "hash": "a" * 64}
    store = body["store"]
    assert store["states"] == ["near_cap"] and store["backend"] == "sqlite"
    assert (store["bytes_used"], store["cap_bytes"]) == (900 * MIB, GIB)
    assert store["reserve_left_bytes"] == RESERVE_BYTES
    assert store["decisions_lost"] == 2
    assert store["oldest_record_at"] == "2027-01-14T00:00:00Z"


def test_over_the_cap_reads_on_reserve_and_eats_into_it() -> None:
    body = _status(usage=_usage(used=GIB + 4 * MIB, state="over_cap"))
    assert errors("Status", body) == []
    assert body["store"]["states"] == ["on_reserve"]
    assert body["store"]["reserve_left_bytes"] == RESERVE_BYTES - 4 * MIB
    deep = _status(usage=_usage(used=GIB + 2 * RESERVE_BYTES, state="over_cap"))
    assert deep["store"]["reserve_left_bytes"] == 0


def test_restarts_older_than_a_day_do_not_count() -> None:
    body = _status(history=History(restarts=(T0 - 86_400 - 1, T0 + 1), last_exit=None))
    assert body["restarts_24h"] == 1


def _state(**over) -> str:
    args = {
        "uptime_s": 3600,
        "restarts_10m": 0,
        "sensors": {"reporting": 3, "cant_see": 0, "off": 0},
        "store_states": ["ok"],
        "decisions_lost": 0,
    }
    args.update(over)
    return medic_state(**args)


@pytest.mark.parametrize(
    ("over", "want"),
    [
        ({}, "running"),
        ({"uptime_s": 299}, "starting"),
        ({"sensors": {"reporting": 2, "cant_see": 1, "off": 0}}, "degraded"),
        ({"store_states": ["near_cap"]}, "degraded"),
        ({"decisions_lost": 1}, "degraded"),
        ({"sensors": {"reporting": 0, "cant_see": 3, "off": 0}}, "blind"),
        ({"store_states": ["recording_stopped"]}, "not_recording"),
        ({"store_states": ["migration_blocked"]}, "not_recording"),
        ({"restarts_10m": 6}, "crash_looping"),
        ({"restarts_10m": 5}, "running"),
        # Every sensor off is not blind: Medic knows why it isn't looking.
        ({"sensors": {"reporting": 0, "cant_see": 0, "off": 2}}, "running"),
    ],
)
def test_state_is_c5s_worst_wins(over, want) -> None:
    assert _state(**over) == want


def test_worst_wins_order() -> None:
    blind = {"reporting": 0, "cant_see": 3, "off": 0}
    assert _state(restarts_10m=9, store_states=["recording_stopped"]) == "crash_looping"
    assert _state(store_states=["recording_stopped"], sensors=blind) == "not_recording"
    # Start-up grace: sensors failing while Vigil comes up are expected (C5 §5.1).
    assert _state(uptime_s=10, sensors=blind) == "starting"
    assert _state(sensors=blind, store_states=["near_cap"]) == "blind"


def test_down_off_and_unknown_are_never_medics() -> None:
    # C5 §5.4: those three are the backend's alone.
    enum = SPEC["components"]["schemas"]["Status"]["properties"]["state"]["enum"]
    assert not {"down", "off", "unknown"} & set(enum)
    for uptime in (0, 10**6):
        assert _state(uptime_s=uptime) in enum


def test_serve_view_sets_now_and_uptime_at_serve_time() -> None:
    body = _status()
    later = serve_view(body, now=T0 + 610.7, started_at=T0)
    assert later["now"] == "2027-01-15T08:10:10Z" and later["uptime_s"] == 610
    assert body["now"] == "2027-01-15T08:10:00Z"  # the snapshot itself is untouched
    assert errors("Status", later) == []


def test_read_chain_empty_and_with_records(tmp_path) -> None:
    with open_writer(tmp_path) as writer:
        assert read_chain(tmp_path) == {
            "head": None,
            "oldest_at": None,
            "newest_at": None,
        }
        for at in ("2027-01-15T08:00:00Z", "2027-01-15T08:00:15Z"):
            last = writer.append(
                {
                    "v": 1,
                    "at": at,
                    "type": "gap",
                    "body": {"from": at, "to": at, "reason": "off"},
                }
            )
    chain = read_chain(tmp_path)
    assert chain == {
        "head": {"seq": last["seq"], "hash": last["hash"]},
        "oldest_at": "2027-01-15T08:00:00Z",
        "newest_at": "2027-01-15T08:00:15Z",
    }


def test_read_chain_without_a_store_is_empty(tmp_path) -> None:
    assert read_chain(tmp_path / "none")["head"] is None
