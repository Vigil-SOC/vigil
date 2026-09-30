"""The one-time switch-on at daemon boot, against the throwaway Postgres."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    from core.storage.models import (
        ConfigAuditLog,
        FederationSource,
        Finding,
        SystemConfig,
    )

    with get_db_manager().session_scope() as session:
        session.query(FederationSource).delete()
        session.query(ConfigAuditLog).filter(
            ConfigAuditLog.config_key.in_([GLOBAL_KEY, DEFAULT_ON_KEY])
        ).delete(synchronize_session=False)
        session.query(Finding).filter(Finding.finding_id.like("upgrade-%")).delete(
            synchronize_session=False
        )
        session.query(SystemConfig).filter(
            SystemConfig.key.in_([GLOBAL_KEY, DEFAULT_ON_KEY])
        ).delete(synchronize_session=False)
    yield


def _seed(**state):
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, Finding, SystemConfig

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
        for n, (source_id, when) in enumerate(state.get("stored", [])):
            session.add(
                Finding(
                    finding_id=f"upgrade-{n}", data_source=source_id, timestamp=when
                )
            )


def _audits():
    from core.storage.connection import get_db_manager
    from core.storage.models import ConfigAuditLog

    with get_db_manager().session_scope() as session:
        entries = (
            session.query(ConfigAuditLog)
            .filter(ConfigAuditLog.config_key.in_([GLOBAL_KEY, DEFAULT_ON_KEY]))
            .order_by(ConfigAuditLog.id)
            .all()
        )
        return [
            (e.config_key, e.action, e.old_value, e.new_value, e.changed_by)
            for e in entries
        ]


def _cursors():
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource

    with get_db_manager().session_scope() as session:
        return {
            r.source_id: (r.cursor or {}).get("last_poll_at")
            for r in session.query(FederationSource).all()
        }


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


def test_a_switched_on_source_resumes_from_the_newest_alert_legacy_stored(adapters):
    now = utcnow()
    legacy_newest = now - timedelta(minutes=7)
    _seed(
        global_enabled=False,
        rows={
            # Federation ran it in August; the legacy loop polled it since.
            "splunk": {
                "enabled": False,
                "interval_seconds": 300,
                "cursor": {"last_poll_at": (now - timedelta(days=40)).isoformat()},
                "last_poll_at": now - timedelta(days=40),
            },
        },
        stored=[
            ("splunk", now - timedelta(minutes=30)),
            ("splunk", legacy_newest),
            ("crowdstrike", now - timedelta(days=3)),
        ],
    )

    apply_default_on()

    cursors = _cursors()
    assert cursors["splunk"] == legacy_newest.isoformat()
    # Stored long ago: no further back than the catch-up limit.
    capped = datetime.fromisoformat(cursors["crowdstrike"])
    assert abs(capped - (now - timedelta(hours=1))) < timedelta(seconds=5)
    # Nothing stored: a cold start, as for a source Federation never saw.
    assert cursors["newsource"] is None


def test_a_row_federation_already_polls_keeps_its_cursor(adapters):
    mine = {"last_poll_at": "2026-09-30T12:00:00"}
    _seed(
        global_enabled=True,
        rows={"splunk": {"enabled": True, "interval_seconds": 120, "cursor": mine}},
        stored=[("splunk", utcnow())],
    )

    apply_default_on()

    assert _cursors()["splunk"] == mine["last_poll_at"]


def test_a_missing_findings_table_still_switches_everything_on(adapters, monkeypatch):
    # A fresh install: the daemon can boot before the backend creates findings.
    from sqlalchemy import Column, DateTime, String
    from sqlalchemy.orm import declarative_base

    class _Missing(declarative_base()):
        __tablename__ = "no_such_findings"
        finding_id = Column(String, primary_key=True)
        data_source = Column(String)
        timestamp = Column(DateTime)

    monkeypatch.setattr("core.storage.models.Finding", _Missing)

    assert sorted(apply_default_on()) == ["crowdstrike", "newsource", "splunk"]

    state = _state()
    assert state["global"] is True
    assert state["marker"] is True
    assert set(_cursors().values()) == {None}


@pytest.mark.parametrize(
    "seeded, action, before",
    [
        ({"global_enabled": False}, "update", {"enabled": False}),
        ({}, "create", None),
    ],
    ids=["global-row-off", "no-global-row"],
)
def test_the_switch_on_is_audited_with_the_marker(adapters, seeded, action, before):
    _seed(**seeded)

    apply_default_on()

    switch, marker = _audits()
    assert switch == (GLOBAL_KEY, action, before, {"enabled": True}, "system")
    key, marker_action, old, new, by = marker
    assert (key, marker_action, old, by) == (DEFAULT_ON_KEY, "create", None, "system")
    assert sorted(new["switched_on"]) == ["crowdstrike", "newsource", "splunk"]


def test_a_global_switch_already_on_audits_only_the_marker(adapters):
    _seed(global_enabled=True)

    apply_default_on()

    assert [(key, action) for key, action, *_ in _audits()] == [
        (DEFAULT_ON_KEY, "create")
    ]


def test_a_row_off_is_switched_on_even_when_its_integration_is_not(adapters):
    # Seeding leaves an existing row alone, so a row left off here would stay
    # off once its integration is configured again; legacy polled it then.
    _seed(
        global_enabled=False,
        rows={
            "elastic": {"enabled": False, "interval_seconds": 300},
            # Its adapter module failed to import this boot.
            "unregistered": {"enabled": False, "interval_seconds": 90},
        },
    )

    assert sorted(apply_default_on()) == [
        "crowdstrike",
        "elastic",
        "newsource",
        "splunk",
        "unregistered",
    ]
    rows = _state()["rows"]
    assert rows["elastic"] == (True, 600)
    assert rows["unregistered"] == (True, 90)


def test_a_failure_writes_nothing_so_setup_retries(adapters, monkeypatch):
    _seed(
        global_enabled=False,
        rows={"splunk": {"enabled": False, "interval_seconds": 300}},
    )

    def unreachable():
        raise RuntimeError("config store unreachable")

    monkeypatch.setattr(adapters[1], "is_configured", unreachable)

    with pytest.raises(RuntimeError):
        apply_default_on()

    assert _state() == {
        "rows": {"splunk": (False, 300)},
        "global": False,
        "marker": False,
    }
