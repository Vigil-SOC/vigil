"""The backend's last-seen poller and its one Postgres row (C5 §5.3, D2-18, V2).

One backend process polls Medic's status op (through the gateway's inbound
listener, with the per-install ``X-Medic-Key``) every 60 s and keeps the result
in ``medic_last_seen``, so the state survives restarts and every replica reads
the same answer. Success sets ``last_seen_at`` and clears the failure; a failure
sets ``first_failed_at`` once and the ``failure_kind``. Flag off: no poll, no row.

Real Postgres (the throwaway unit database); Medic is a fake ``fetch`` or an
``httpx.MockTransport``; the clock is passed in.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, text

from core.config import Settings
from core.platform import medic_last_seen as mls
from core.platform.medic_status import MedicStatus
from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

T0 = datetime(2026, 10, 8, 12, 0, 0)
KEY = "k" * 43
URL = "http://medic-gateway-in:8470"
STATUS = {
    "api_version": "1.0",
    "instance_id": "mi_3f9a2c1b0d4e5f60",
    "state": "degraded",
    "now": "2031-01-01T00:00:00Z",
    "heartbeat_at": "2026-10-08T11:59:58Z",
    "cycle": 20841,
    "started_at": "2026-10-04T09:58:02Z",
    "uptime_s": 273269,
    "restarts_24h": 2,
    "last_exit": {"reason": "oom", "at": "2026-10-08T08:11:40Z"},
    "dev_mode": False,
    "store": {"state": "ok"},
    "sensors": {"reporting": 3, "cant_see": 1, "off": 0},
    "pack": None,
    "chain_head": None,
}


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "medic_backend_api_key"
    path.write_text(KEY + "\n")
    return path


@pytest.fixture
def on(key_file):
    return Settings(
        _env_file=None,
        vigil_medic_enabled="true",
        vigil_medic_api_url=URL,
        vigil_medic_api_key_file=str(key_file),
    )


@pytest.fixture(autouse=True)
def empty_table():
    with get_db_manager().session_scope() as s:
        s.execute(text("DELETE FROM medic_last_seen"))
    yield


def _row():
    with get_db_manager().session_scope() as s:
        return (
            s.execute(
                text(
                    "SELECT last_seen_at, first_failed_at, failure_kind, "
                    "status_snapshot, updated_at FROM medic_last_seen"
                )
            )
            .mappings()
            .all()
        )


def _ok(url, key):
    return mls.Outcome(snapshot=mls.typed_snapshot(STATUS), failure=None)


def _fail(kind):
    def fetch(url, key):
        return mls.Outcome(snapshot=None, failure=mls.FailureKind(kind))

    return fetch


# --- the row ---------------------------------------------------------------


def test_success_sets_last_seen_and_a_typed_snapshot(on):
    assert mls.poll_once(on, now=T0, fetch=_ok) is True
    [row] = _row()
    assert row["last_seen_at"] == T0
    assert row["updated_at"] == T0
    assert row["first_failed_at"] is None and row["failure_kind"] is None
    snap = row["status_snapshot"]
    assert snap["state"] == "degraded"
    assert snap["sensors"] == {"reporting": 3, "cant_see": 1, "off": 0}
    # Typed fields only: Medic's clock and the blocks H3 serves live are not kept.
    assert "now" not in snap and "store" not in snap and "pack" not in snap
    assert mls.current_status(on, now=_at(30)) is MedicStatus.RUNNING


@pytest.mark.parametrize("kind", ["refused", "timeout", "401", "5xx"])
def test_failure_kind_and_a_stable_first_failed_at(on, kind):
    mls.poll_once(on, now=T0, fetch=_ok)
    for n in range(1, 5):
        assert mls.poll_once(on, now=_at(60 * n), fetch=_fail(kind)) is True
    [row] = _row()
    assert row["first_failed_at"] == _at(60)
    assert row["failure_kind"] == kind
    assert row["last_seen_at"] == T0
    assert row["updated_at"] == _at(240)


def test_the_latest_failure_kind_wins_but_first_failed_at_stays(on):
    mls.poll_once(on, now=T0, fetch=_fail("refused"))
    mls.poll_once(on, now=_at(60), fetch=_fail("timeout"))
    [row] = _row()
    assert (row["first_failed_at"], row["failure_kind"]) == (T0, "timeout")


def test_recovery_clears_the_failure(on):
    mls.poll_once(on, now=T0, fetch=_fail("refused"))
    mls.poll_once(on, now=_at(60), fetch=_ok)
    [row] = _row()
    assert row["last_seen_at"] == _at(60)
    assert row["first_failed_at"] is None and row["failure_kind"] is None
    # The last good snapshot is kept through a failure, replaced on recovery.
    mls.poll_once(on, now=_at(120), fetch=_fail("5xx"))
    [row] = _row()
    assert row["status_snapshot"]["state"] == "degraded"


def test_kill_medic_reads_down_within_five_minutes(on):
    """Fake clock, one poll a minute: running, then Medic dies just after a
    success; the stored row must read Down by kill + 300 s."""
    kill = _at(1)
    for tick in range(0, 660, 60):
        now = _at(tick)
        mls.poll_once(on, now=now, fetch=_ok if now < kill else _fail("refused"))
        for second in range(tick, tick + 60, 5):
            if mls.current_status(on, now=_at(second)) is MedicStatus.DOWN:
                assert _at(second) - kill <= timedelta(seconds=300)
                return
    raise AssertionError("never Down")


def test_the_state_survives_a_backend_restart(on):
    """A new process (here: a new engine on the same database) reads the row."""
    mls.poll_once(on, now=T0, fetch=_ok)
    for n in range(1, 6):
        mls.poll_once(on, now=_at(60 * n), fetch=_fail("timeout"))
    url = get_db_manager()._engine.url
    fresh = create_engine(url)
    try:
        with fresh.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT last_seen_at, first_failed_at, failure_kind FROM medic_last_seen"
                )
            ).one()
    finally:
        fresh.dispose()
    assert tuple(row) == (T0, _at(60), "timeout")
    assert mls.current_status(on, now=_at(300)) is MedicStatus.DOWN


# --- one poller --------------------------------------------------------------


def test_two_replicas_at_once_poll_once(on):
    calls = []
    barrier = threading.Barrier(2)

    def slow(url, key):
        calls.append(threading.get_ident())
        time.sleep(0.5)
        return _ok(url, key)

    def replica(out):
        barrier.wait()
        out.append(mls.poll_once(on, now=T0, fetch=slow))

    results: list[bool] = []
    threads = [threading.Thread(target=replica, args=(results,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert sorted(results) == [False, True]


def test_a_second_replica_in_the_same_interval_skips(on):
    calls = []

    def counted(url, key):
        calls.append(1)
        return _ok(url, key)

    assert mls.poll_once(on, now=T0, fetch=counted) is True
    # Replica B ticks 20 s later (its own 60 s timer): the row is fresh.
    assert mls.poll_once(on, now=_at(20), fetch=counted) is False
    assert mls.poll_once(on, now=_at(54), fetch=counted) is False
    assert mls.poll_once(on, now=_at(60), fetch=counted) is True
    assert len(calls) == 2


def test_two_replicas_on_offset_timers_make_one_poll_a_minute(on):
    calls = []

    def counted(url, key):
        calls.append(1)
        return _ok(url, key)

    for minute in range(10):
        for offset in (0, 23):  # replica A at :00, replica B at :23
            mls.poll_once(on, now=_at(60 * minute + offset), fetch=counted)
    assert len(calls) == 10


# --- flag off ----------------------------------------------------------------


def test_flag_off_no_poll_no_row(key_file):
    off = Settings(
        _env_file=None,
        vigil_medic_enabled="false",
        vigil_medic_api_url=URL,
        vigil_medic_api_key_file=str(key_file),
    )

    def never(url, key):
        raise AssertionError("polled with the flag off")

    assert mls.poll_once(off, now=T0, fetch=never) is False
    assert _row() == []
    assert mls.current_status(off, now=T0) is MedicStatus.OFF
    assert mls.start_poller(off) is None


def test_flag_on_and_never_polled_is_unknown(on):
    assert mls.current_status(on, now=T0) is MedicStatus.UNKNOWN


def test_postgres_down_reads_unknown(on, monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(mls, "read_last_seen", broken)
    assert mls.current_status(on, now=T0) is MedicStatus.UNKNOWN


# --- what a poll sends and how it classifies the answer ------------------------


def _fetch_with(handler, on):
    return mls.fetch_status(
        URL, mls.read_key(on), transport=httpx.MockTransport(handler)
    )


def test_the_request_is_the_status_op_with_the_key_and_nothing_else(on):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=STATUS)

    out = _fetch_with(handler, on)
    assert out.failure is None and out.snapshot["state"] == "degraded"
    [req] = seen
    assert (req.method, str(req.url)) == ("GET", URL + "/v1/status")
    assert req.headers["X-Medic-Key"] == KEY
    assert "authorization" not in req.headers and "cookie" not in req.headers


@pytest.mark.parametrize(
    "status, body, kind",
    [
        (401, {"code": "unauthorized"}, "401"),
        (403, {"code": "forbidden"}, "401"),
        (500, {"code": "internal"}, "5xx"),
        (503, {"code": "busy"}, "5xx"),
        # The gateway's own verdicts on the hop to Medic (services/medic_gateway).
        (502, {"code": "upstream_unreachable"}, "refused"),
        (504, {"code": "upstream_timeout"}, "timeout"),
        (502, {"code": "upstream_content_type"}, "5xx"),
        (404, {"code": "not_found"}, "5xx"),
        (200, {"state": "running"}, "5xx"),  # not Medic's status shape
        (200, {**STATUS, "state": "down"}, "5xx"),  # Medic never says down
        (200, {**STATUS, "uptime_s": "x" * 5000}, "5xx"),
    ],
)
def test_answers_map_to_a_failure_kind(on, status, body, kind):
    out = _fetch_with(lambda r: httpx.Response(status, json=body), on)
    assert out.snapshot is None
    assert out.failure is mls.FailureKind(kind)


def test_a_non_json_answer_is_5xx(on):
    out = _fetch_with(lambda r: httpx.Response(200, text="<html>"), on)
    assert out.failure is mls.FailureKind.SERVER_ERROR


def test_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = mls.fetch_status(f"http://127.0.0.1:{port}", KEY, timeout=2.0)
    assert out.failure is mls.FailureKind.REFUSED


def test_timeout():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)  # accepts, never answers
        port = s.getsockname()[1]
        out = mls.fetch_status(f"http://127.0.0.1:{port}", KEY, timeout=0.5)
    assert out.failure is mls.FailureKind.TIMEOUT


def test_no_key_is_401_and_sends_nothing(tmp_path):
    settings = Settings(
        _env_file=None,
        vigil_medic_enabled="true",
        vigil_medic_api_url=URL,
        vigil_medic_api_key_file=str(tmp_path / "missing"),
    )
    assert mls.read_key(settings) is None
    out = mls.fetch_status(URL, None, transport=httpx.MockTransport(_explode))
    assert out.failure is mls.FailureKind.UNAUTHORIZED


def test_no_url_is_refused_and_sends_nothing():
    out = mls.fetch_status("", KEY, transport=httpx.MockTransport(_explode))
    assert out.failure is mls.FailureKind.REFUSED


def _explode(request):
    raise AssertionError("sent a request")


@pytest.mark.parametrize(
    "bad", ["", "short", "k" * 64, "k" * 43 + " extra", "k" * 42 + "=", "é" * 43]
)
def test_a_malformed_key_file_reads_as_no_key(tmp_path, bad):
    path = tmp_path / "key"
    path.write_text(bad)
    settings = Settings(
        _env_file=None, vigil_medic_enabled="true", vigil_medic_api_key_file=str(path)
    )
    assert mls.read_key(settings) is None


def test_the_snapshot_is_capped(on):
    snap = mls.typed_snapshot(STATUS)
    assert len(mls.snapshot_bytes(snap)) <= mls.SNAPSHOT_MAX_BYTES


# --- the loop and its place in the backend's lifespan --------------------------


@pytest.mark.asyncio
async def test_the_loop_polls_every_interval_and_survives_errors(on, monkeypatch):
    import asyncio

    calls = []

    def flaky(settings):
        calls.append(settings)
        if len(calls) == 1:
            raise RuntimeError("db blip")
        return True

    monkeypatch.setattr(mls, "poll_once", flaky)
    task = asyncio.create_task(mls.run_poller(on, interval=0.01))
    while len(calls) < 3:
        await asyncio.sleep(0.01)
    task.cancel()
    assert all(s is on for s in calls)


@pytest.mark.asyncio
async def test_the_backend_starts_one_task_and_stops_it(on, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from services.api import main

    started = asyncio.Event()

    async def fake_loop(settings, interval=60):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(mls, "run_poller", fake_loop)
    monkeypatch.setattr(main, "get_settings", lambda: on)
    app = SimpleNamespace(state=SimpleNamespace())
    main._start_medic_poller(app)
    await asyncio.wait_for(started.wait(), 1)
    task = app.state.medic_poller
    await main._stop_medic_poller(app)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_the_backend_starts_nothing_with_the_flag_off(monkeypatch):
    from types import SimpleNamespace

    from services.api import main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    app = SimpleNamespace(state=SimpleNamespace())
    main._start_medic_poller(app)
    assert app.state.medic_poller is None
    await main._stop_medic_poller(app)


def test_startup_and_shutdown_call_them():
    import inspect

    from services.api import main

    assert "_start_medic_poller(app)" in inspect.getsource(main._startup)
    assert "await _stop_medic_poller(app)" in inspect.getsource(main._shutdown)


# --- review fixes: stale rows, the database's clock, saying why -----------------


def test_re_enabling_starts_a_new_outage_not_an_old_one(on):
    """Off for an hour with a failure stored: the first poll back starts afresh."""
    mls.poll_once(on, now=T0, fetch=_ok)
    mls.poll_once(on, now=_at(60), fetch=_fail("refused"))
    back = _at(3600)
    assert mls.current_status(on, now=back) is MedicStatus.UNKNOWN  # stale row
    mls.poll_once(on, now=back, fetch=_fail("refused"))
    [row] = _row()
    assert row["first_failed_at"] == back
    assert mls.current_status(on, now=back + timedelta(seconds=60)) is (
        MedicStatus.UNKNOWN
    )


def test_times_come_from_the_database_not_this_replicas_clock(on, monkeypatch):
    """A replica whose clock is an hour off still writes and reads one clock."""
    import core.time
    from core.platform import medic_status as ms

    for module in (ms, core.time):
        monkeypatch.setattr(module, "utcnow", lambda: datetime(2001, 1, 1))
    with get_db_manager().session_scope() as s:
        db_now = s.execute(text("SELECT now() AT TIME ZONE 'UTC'")).scalar()
    assert mls.poll_once(on, fetch=_ok) is True
    [row] = _row()
    assert abs(row["updated_at"] - db_now) < timedelta(seconds=30)
    assert mls.current_status(on) is MedicStatus.RUNNING


def test_a_missing_url_or_key_is_said_once_at_start(tmp_path, caplog):
    import asyncio

    settings = Settings(
        _env_file=None,
        vigil_medic_enabled="true",
        vigil_medic_api_url="",
        vigil_medic_api_key_file=str(tmp_path / "missing"),
    )

    async def start_and_stop():
        task = mls.start_poller(settings)
        task.cancel()

    with caplog.at_level("WARNING", logger=mls.logger.name):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mls, "run_poller", _forever)
            asyncio.run(start_and_stop())
    text_ = caplog.text
    assert "VIGIL_MEDIC_API_URL" in text_ and "VIGIL_MEDIC_API_KEY_FILE" in text_


def test_a_good_config_says_nothing(on, caplog):
    import asyncio

    async def start_and_stop():
        mls.start_poller(on).cancel()

    with caplog.at_level("WARNING", logger=mls.logger.name):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mls, "run_poller", _forever)
            asyncio.run(start_and_stop())
    assert caplog.text == ""
    assert KEY not in caplog.text


async def _forever(settings, interval=60):
    import asyncio

    await asyncio.sleep(3600)


def test_the_first_failure_logs_the_http_status_never_the_body(on, caplog):
    def handler(request):
        return httpx.Response(429, json={"code": "slow_down " + KEY})

    def fetch(url, key):
        return mls.fetch_status(url, key, transport=httpx.MockTransport(handler))

    with caplog.at_level("WARNING", logger=mls.logger.name):
        mls.poll_once(on, now=T0, fetch=fetch)
        mls.poll_once(on, now=_at(60), fetch=fetch)
    assert caplog.text.count("Medic status poll failed") == 1
    assert "5xx" in caplog.text and "429" in caplog.text
    assert KEY not in caplog.text and "slow_down" not in caplog.text


@pytest.mark.asyncio
async def test_the_loop_keeps_a_fixed_cadence_when_a_poll_is_slow(on, monkeypatch):
    import asyncio

    starts = []

    def slow(settings):
        starts.append(time.monotonic())
        time.sleep(0.05)

    monkeypatch.setattr(mls, "poll_once", slow)
    task = asyncio.create_task(mls.run_poller(on, interval=0.1))
    while len(starts) < 4:
        await asyncio.sleep(0.01)
    task.cancel()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(0.08 < g < 0.13 for g in gaps), gaps
