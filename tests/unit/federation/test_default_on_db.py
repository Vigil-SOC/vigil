"""The one-time switch-on at daemon boot, against the throwaway Postgres."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.federation.seed import DEFAULT_ON_KEY, apply_default_on
from core.federation.store import GLOBAL_KEY
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


class _Adapter:
    def __init__(self, name: str, *, configured: bool = True, interval: int = 45):
        self.name = name
        self._configured = configured
        self._interval = interval

    def is_configured(self) -> bool:
        return self._configured

    def default_interval(self) -> int:
        return self._interval


@pytest.fixture
def adapters(monkeypatch):
    listed = [
        _Adapter("splunk"),
        _Adapter("crowdstrike"),
        _Adapter("elastic", configured=False),
        _Adapter("newsource"),
    ]
    monkeypatch.setattr("core.federation.seed.list_adapters", lambda: listed)
    monkeypatch.setattr(
        "core.federation.seed.get_settings",
        lambda: SimpleNamespace(
            daemon_splunk_poll_interval=600, daemon_crowdstrike_poll_interval=30
        ),
    )
    return listed


@pytest.fixture(autouse=True)
def clean_state():
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, SystemConfig

    with get_db_manager().session_scope() as session:
        session.query(FederationSource).delete()
        session.query(SystemConfig).filter(
            SystemConfig.key.in_([GLOBAL_KEY, DEFAULT_ON_KEY])
        ).delete(synchronize_session=False)
    yield


def _seed(**state):
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, SystemConfig

    with get_db_manager().session_scope() as session:
        if "global_enabled" in state:
            session.add(
                SystemConfig(
                    key=GLOBAL_KEY,
                    value={"enabled": state["global_enabled"]},
                    config_type="federation",
                )
            )
        if state.get("marker"):
            session.add(
                SystemConfig(key=DEFAULT_ON_KEY, value={}, config_type="federation")
            )
        for source_id, fields in state.get("rows", {}).items():
            session.add(FederationSource(source_id=source_id, **fields))


def _state():
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, SystemConfig

    with get_db_manager().session_scope() as session:
        rows = {
            r.source_id: (r.enabled, r.interval_seconds)
            for r in session.query(FederationSource).all()
        }
        global_row = session.get(SystemConfig, GLOBAL_KEY)
        marker = session.get(SystemConfig, DEFAULT_ON_KEY)
        return {
            "rows": rows,
            "global": (global_row.value or {}).get("enabled") if global_row else None,
            "marker": marker is not None,
        }


def test_an_install_nobody_used_is_switched_on_at_the_legacy_cadence(adapters):
    _seed(
        global_enabled=False,
        rows={"splunk": {"enabled": False, "interval_seconds": 300}},
    )

    switched = apply_default_on()

    assert sorted(switched) == ["crowdstrike", "newsource", "splunk"]
    state = _state()
    assert state["global"] is True
    assert state["rows"] == {
        "splunk": (True, 600),
        "crowdstrike": (True, 30),
        # Not a legacy source: its adapter's own default.
        "newsource": (True, 45),
    }
    assert state["marker"] is True


def test_rows_federation_already_polls_are_left_alone(adapters):
    # A disabled row was still polled, by its legacy loop.
    _seed(
        global_enabled=True,
        rows={
            "splunk": {"enabled": True, "interval_seconds": 120},
            "crowdstrike": {"enabled": False, "interval_seconds": 300},
        },
    )

    assert sorted(apply_default_on()) == ["crowdstrike", "newsource"]

    state = _state()
    assert state["global"] is True
    assert state["rows"] == {
        "splunk": (True, 120),
        "crowdstrike": (True, 30),
        "newsource": (True, 45),
    }
    assert state["marker"] is True


@pytest.mark.parametrize(
    "row, switched",
    [
        ({"enabled": True, "interval_seconds": 120}, ["newsource", "splunk"]),
        (
            {"enabled": False, "interval_seconds": 120, "last_poll_at": utcnow()},
            ["crowdstrike", "newsource", "splunk"],
        ),
    ],
    ids=["a-row-enabled", "a-row-polled"],
)
def test_a_paused_install_is_switched_on_and_keeps_its_settings(
    adapters, row, switched
):
    # Paused, the legacy loops polled every source; switching on keeps that.
    _seed(global_enabled=False, rows={"crowdstrike": row})

    assert sorted(apply_default_on()) == switched

    state = _state()
    assert state["global"] is True
    assert state["rows"] == {
        # Federation ran it: its own interval, not the legacy one.
        "crowdstrike": (True, 120),
        "splunk": (True, 600),
        "newsource": (True, 45),
    }
    assert state["marker"] is True


def test_it_runs_once_so_a_later_switch_off_survives_a_restart(adapters):
    _seed(global_enabled=False, marker=True)

    assert apply_default_on() == []

    state = _state()
    assert state["global"] is False
    assert state["rows"] == {}
