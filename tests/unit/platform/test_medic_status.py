"""Off is not Down: the backend reads the same master switch as Medic (C8 rule 1).

``VIGIL_MEDIC_ENABLED`` false → ``off``, and nothing is polled. True → ``unknown``
until the poller has seen Medic, then ``running`` or ``down`` from its row (V2). The backend
must read a value exactly as Medic does (``services/medic/app/config.py``):
otherwise one says off while the other runs, and the console shows the wrong one.
"""

import socket
from datetime import datetime, timedelta

import pytest

from core.config import Settings
from core.platform import medic_status as ms
from services.medic.app import config as medic_config

pytestmark = pytest.mark.unit

VALUES = [
    None,
    "",
    "true",
    "TRUE",
    " True ",
    "1",
    "yes",
    "on",
    "false",
    "0",
    "no",
    "off",
    "t",
    "y",
    "enabled",
    "maybe",
    "2",
]


def _settings(value):
    if value is None:
        return Settings(_env_file=None)
    return Settings(_env_file=None, vigil_medic_enabled=value)


def test_off_by_default():
    assert ms.medic_status(_settings(None)) is ms.MedicStatus.OFF
    assert ms.medic_status(_settings(None)).value == "off"


def test_on_is_unknown_until_v2_has_seen_it():
    assert ms.medic_status(_settings("true")) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_settings("true")).value == "unknown"


@pytest.mark.parametrize("value", VALUES)
def test_backend_and_medic_read_every_value_the_same(value):
    env = {} if value is None else {medic_config.ENABLED_VAR: value}
    assert ms.medic_enabled(_settings(value)) is medic_config.is_enabled(env)


@pytest.mark.parametrize("value", ["maybe", "t", "2"])
def test_an_unrecognised_value_is_off_and_does_not_break_settings(value):
    assert ms.medic_status(_settings(value)) is ms.MedicStatus.OFF


def test_the_env_var_is_the_one_medic_reads(monkeypatch):
    monkeypatch.setenv(medic_config.ENABLED_VAR, "true")
    assert ms.medic_status(Settings(_env_file=None)) is ms.MedicStatus.UNKNOWN


@pytest.mark.parametrize("value", ["false", "true"])
def test_no_polling(monkeypatch, value):
    """Reading the status opens no connection, on or off (V2 adds the poller)."""

    def refuse(*a, **k):
        raise AssertionError("medic_status opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    ms.medic_status(_settings(value))


def test_reads_the_process_settings_when_none_given(monkeypatch):
    monkeypatch.setattr(ms, "get_settings", lambda: _settings("on"))
    assert ms.medic_status() is ms.MedicStatus.UNKNOWN


# --- V2: running and down, from the backend's last-seen row (C5 §5.3, D2-18) ---

T0 = datetime(2026, 10, 8, 12, 0, 0)


def _on():
    return _settings("true")


def _seen(last=None, failed=None, kind=None):
    return ms.LastSeen(last_seen_at=last, first_failed_at=failed, failure_kind=kind)


def _at(seconds):
    return T0 + timedelta(seconds=seconds)


def test_never_seen_is_unknown():
    assert ms.medic_status(_on(), None, T0) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), _seen(), T0) is ms.MedicStatus.UNKNOWN


@pytest.mark.parametrize("age", [0, 1, 60, 119])
def test_seen_within_two_polls_is_running(age):
    seen = _seen(last=T0)
    assert ms.medic_status(_on(), seen, _at(age)) is ms.MedicStatus.RUNNING


def test_one_failure_after_a_recent_success_is_still_running():
    seen = _seen(last=T0, failed=_at(60), kind="refused")
    assert ms.medic_status(_on(), seen, _at(61)) is ms.MedicStatus.RUNNING


def test_not_seen_for_two_polls_and_not_yet_down_is_unknown():
    seen = _seen(last=T0, failed=_at(60), kind="refused")
    assert ms.medic_status(_on(), seen, _at(120)) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), seen, _at(299)) is ms.MedicStatus.UNKNOWN


@pytest.mark.parametrize("kind", ["refused", "timeout", "401", "5xx"])
def test_failing_and_unseen_for_five_minutes_is_down(kind):
    seen = _seen(last=T0, failed=_at(60), kind=kind)
    assert ms.medic_status(_on(), seen, _at(300)) is ms.MedicStatus.DOWN


def test_killed_medic_reads_down_within_five_minutes_of_the_kill():
    """Polls every 60 s with a fake clock: the kill lands just after a success,
    the worst case, and Down must still show by kill + 300 s."""
    kill = _at(1)
    last, failed = T0, None
    status = None
    for tick in range(0, 601, ms.POLL_INTERVAL_S):
        now = _at(tick)
        if now < kill:
            last = now
        elif failed is None:
            failed = now
        for second in range(tick, tick + ms.POLL_INTERVAL_S):
            status = ms.medic_status(_on(), _seen(last, failed, "refused"), _at(second))
            if status is ms.MedicStatus.DOWN:
                assert _at(second) - kill <= timedelta(seconds=300)
                return
    raise AssertionError(f"never Down; last {status}")


def test_a_backend_back_after_a_long_gap_does_not_flash_down():
    """Last seen a day ago (the backend was off), one refused poll now: a
    restarting Medic gets the same grace as any other outage."""
    seen = _seen(last=T0, failed=_at(86_400), kind="refused")
    assert ms.medic_status(_on(), seen, _at(86_400)) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), seen, _at(86_400 + 229)) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), seen, _at(86_400 + 230)) is ms.MedicStatus.DOWN


def test_never_seen_and_failing_is_down_after_the_grace():
    seen = _seen(failed=T0, kind="timeout")
    assert ms.medic_status(_on(), seen, _at(229)) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), seen, _at(230)) is ms.MedicStatus.DOWN


def test_a_stale_row_without_failures_is_unknown_not_down():
    """Nobody is polling (no failure recorded): the backend can't tell."""
    seen = _seen(last=T0)
    assert ms.medic_status(_on(), seen, _at(3600)) is ms.MedicStatus.UNKNOWN


def test_off_wins_over_any_row():
    seen = _seen(last=T0, failed=_at(60), kind="refused")
    assert ms.medic_status(_settings("false"), seen, _at(900)) is ms.MedicStatus.OFF


@pytest.mark.parametrize("rhythm", [60, 70])  # 70: polls further apart than planned
@pytest.mark.parametrize("kill_after", [1, 30, 59])
def test_down_by_kill_plus_300_for_any_kill_time(kill_after, rhythm):
    kill = _at(120 + kill_after)
    last = _at(120)  # the last tick before the kill answered
    failed = None
    tick = 120 + rhythm
    while tick < 1200:
        if failed is None:
            failed = _at(tick)
        for second in range(tick, tick + rhythm):
            seen = ms.LastSeen(last, failed, "timeout", updated_at=_at(tick))
            if ms.medic_status(_on(), seen, _at(second)) is ms.MedicStatus.DOWN:
                assert _at(second) - kill <= timedelta(seconds=300)
                return
        tick += rhythm
    raise AssertionError("never Down")


def test_a_row_nobody_has_written_for_three_polls_is_unknown():
    """Medic was off (or every poller stopped): an old failure says nothing now."""
    seen = ms.LastSeen(T0, _at(60), "refused", updated_at=_at(120))
    assert ms.medic_status(_on(), seen, _at(299)) is ms.MedicStatus.UNKNOWN
    assert ms.medic_status(_on(), seen, _at(300)) is ms.MedicStatus.UNKNOWN
    fresh = ms.LastSeen(T0, _at(60), "refused", updated_at=_at(299))
    assert ms.medic_status(_on(), fresh, _at(300)) is ms.MedicStatus.DOWN
