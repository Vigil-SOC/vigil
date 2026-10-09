"""Off is not Down: the backend reads the same master switch as Medic (C8 rule 1).

``VIGIL_MEDIC_ENABLED`` false → ``off``, and nothing is polled. True → ``unknown``
until V2's poller has seen Medic (it adds ``running`` and ``down``). The backend
must read a value exactly as Medic does (``services/medic/app/config.py``):
otherwise one says off while the other runs, and the console shows the wrong one.
"""

import socket

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
