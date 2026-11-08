"""The promotion cap and the shadow exit rules are stored settings (#4058): the
sweep reads them on every run, the API changes them with validation and an audit
entry, and an unreadable or invalid value is the default, never a crash.

The Postgres test at the end runs when VIGIL_TEST_PG_DSN names a throwaway
database, e.g. postgresql://postgres:t@127.0.0.1:55458/t."""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from core.detections.inventory import metrics, rollout, rollout_limits
from core.storage import config_service as config_store
from services.api.routers import autonomy as routes

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
DEFAULTS = rollout_limits.DEFAULTS
WEEK = lambda days: [  # noqa: E731
    (NOW - timedelta(days=i)).date().isoformat() for i in range(days)
]


class Store:
    """A stored system config the limits read from; ``box['value']`` is the row."""

    def __init__(self, monkeypatch):
        self.value = None
        self.fail = False
        store = self
        monkeypatch.setattr(
            config_store,
            "get_config_service",
            lambda user_id="system": SimpleNamespace(read_system_config=store.read),
        )

    def read(self, key):
        assert key == rollout_limits.LIMITS_KEY
        if self.fail:
            raise RuntimeError("database down")
        return self.value


@pytest.fixture
def store(monkeypatch):
    return Store(monkeypatch)


def _copy(rule_id, days_in=8, hits=3):
    return rollout.Copy(
        rule_id=rule_id,
        version=2,
        name=f"rule {rule_id}",
        language="elastic",
        stage="shadow",
        stage_at=NOW - timedelta(days=days_in),
        checks={
            "runs": {"days": {d: {"pass": 4, "fail": 0} for d in WEEK(30)}},
            "shadow": {"days": {d: hits for d in WEEK(30)}},
            "sees": {"result": "pass"},
            "lint": {"result": "pass", "findings": []},
        },
        rule={},
        key=f"key-{rule_id}",
        toml="[rule]\n",
        path=f"{rule_id}.toml",
    )


def _sweep(monkeypatch, copies):
    inserted = []

    class _Db:
        @contextmanager
        def session_scope(self):
            yield SimpleNamespace(
                scalar=lambda q: 0,
                execute=lambda q, rows=None: inserted.extend(rows or []),
            )

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {})
    monkeypatch.setattr(rollout, "_deploy", lambda *a: None)
    monkeypatch.setattr(rollout, "_noise", lambda s, c: (0, 0))
    monkeypatch.setattr(rollout, "_copies", lambda: copies)
    report = rollout.RolloutReport()
    rollout.sweep(NOW, report)
    return report, inserted


# --- the sweep reads the stored values on every run --------------------------


def test_defaults_are_todays_values(store):
    assert rollout_limits.read_limits() == rollout_limits.RolloutLimits(5, 7, 20)


def test_the_cap_changes_the_next_sweeps_promotions(store, monkeypatch):
    copies = [_copy(i) for i in (1, 2, 3)]
    store.value = {"live_per_day": 1}
    report, rows = _sweep(monkeypatch, copies)
    assert len(report.moves) == 1 and len(rows) == 1
    store.value = {"live_per_day": 0}
    report, rows = _sweep(monkeypatch, copies)
    assert report.moves == [] and rows == []
    store.value = None  # the next sweep sees the new value, no restart
    report, _ = _sweep(monkeypatch, copies)
    assert len(report.moves) == 3


def test_the_shadow_days_change_which_copies_are_due(store, monkeypatch):
    seven_old = _copy(1, days_in=7)
    store.value = {"shadow_days": 14}
    assert _sweep(monkeypatch, [seven_old])[0].moves == []
    store.value = None
    assert len(_sweep(monkeypatch, [seven_old])[0].moves) == 1
    three_old = _copy(2, days_in=3)
    assert _sweep(monkeypatch, [three_old])[0].moves == []
    store.value = {"shadow_days": 3}
    report, rows = _sweep(monkeypatch, [three_old])
    assert len(report.moves) == 1
    assert "3 days in shadow" in rows[0]["reason"]
    assert len(rows[0]["proof"]["days"]) == 3


def test_the_max_hits_change_the_result(store, monkeypatch):
    loud = _copy(1, hits=30)
    assert _sweep(monkeypatch, [loud])[0].moves == []
    store.value = {"max_hits_per_day": 50}
    report, rows = _sweep(monkeypatch, [loud])
    assert len(report.moves) == 1 and "above 50 hits" in rows[0]["reason"]
    store.value = {"max_hits_per_day": 2}
    assert _sweep(monkeypatch, [_copy(2, hits=3)])[0].moves == []


def test_a_live_day_over_the_stored_hits_goes_back_to_shadow(store, monkeypatch):
    monkeypatch.setattr(rollout, "_noise", lambda s, c: (0, 0))
    live = _copy(1)
    live.stage = "live"
    live.checks["live"] = {"days": {NOW.date().isoformat(): 30}}
    limits = rollout_limits.RolloutLimits(max_hits_per_day=50)
    assert rollout._due(None, live, NOW, 0)[0] == "shadow"  # 30 > default 20
    assert rollout._due(None, live, NOW, 0, limits) is None  # 30 <= 50


def test_the_metrics_text_names_the_stored_numbers(store):
    store.value = {"shadow_days": 14, "max_hits_per_day": 9}
    text = metrics.shadow_to_live([], NOW)["exit_criteria"]
    assert text.startswith("14 days in shadow") and "above 9 hits" in text


# --- an unreadable or invalid stored value is the default --------------------


def test_an_unreadable_row_is_the_defaults_and_logs(store, caplog):
    store.fail = True
    with caplog.at_level(logging.ERROR):
        assert rollout_limits.read_limits() == DEFAULTS
    assert "defaults apply" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "lots",
        ["x"],
        {"live_per_day": -1},
        {"live_per_day": "5"},
        {"live_per_day": True},
        {"live_per_day": 1001},
        {"shadow_days": 0},
        {"shadow_days": 91},
        {"shadow_days": 2.5},
        {"max_hits_per_day": 0},
        {"max_hits_per_day": None},
    ],
)
def test_an_invalid_stored_value_is_the_default_and_logs(store, caplog, bad):
    store.value = bad
    with caplog.at_level(logging.ERROR):
        assert rollout_limits.read_limits() == DEFAULTS
    assert caplog.text


def test_one_bad_field_does_not_spoil_the_others(store):
    store.value = {"live_per_day": 2, "shadow_days": "x", "max_hits_per_day": 9}
    assert rollout_limits.read_limits() == rollout_limits.RolloutLimits(2, 7, 9)


def test_the_sweep_survives_an_unreadable_row(store, monkeypatch):
    store.fail = True
    assert len(_sweep(monkeypatch, [_copy(1)])[0].moves) == 1


# --- the API -----------------------------------------------------------------

PERSON = SimpleNamespace(user_id="u-ana", principal_id="p-ana")
SERVICE = SimpleNamespace(user_id="u-svc", principal_id="p-svc")


@pytest.fixture
def api(store, monkeypatch):
    """The routes with the permission, the write and the integration reads faked."""
    seen = SimpleNamespace(perms=[], audit=[])
    monkeypatch.setattr(
        routes, "deny_unless", lambda user, perm, session: seen.perms.append(perm)
    )
    monkeypatch.setattr(routes, "_is_service_account", lambda user: user is SERVICE)
    monkeypatch.setattr(
        rollout_limits,
        "siem_thresholds",
        lambda: {"elastic-siem": {"quiet_max_alerts_per_day": 40}, "splunk": None},
    )

    class _Session:
        row = None  # the row an insert added

        def execute(self, q):
            row = self.row
            if row is None and store.value is not None:
                row = SimpleNamespace(value=store.value, updated_by=None)
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        def add(self, row):
            self.row = row

    session = _Session()

    @contextmanager
    def uow():
        yield session

    monkeypatch.setattr(rollout_limits, "unit_of_work", uow)

    def audit(self, sess, **kw):
        seen.audit.append({"by": self.user_id, **kw})
        store.value = kw["new_value"]

    monkeypatch.setattr(config_store.ConfigService, "_create_audit_log", audit)
    seen.session = session
    return seen


def put(user=PERSON, **body):
    return routes.put_rollout_limits(routes.RolloutLimitsBody(**body), object(), user)


def test_get_returns_stored_values_defaults_and_the_siem_block(store, api):
    store.value = {"live_per_day": 2}
    out = routes.get_rollout_limits()
    assert out.limits == {"live_per_day": 2, "shadow_days": 7, "max_hits_per_day": 20}
    assert out.defaults == {"live_per_day": 5, "shadow_days": 7, "max_hits_per_day": 20}
    assert out.bounds["shadow_days"] == {"min": 1, "max": 90}
    assert out.siem_thresholds["elastic-siem"]["quiet_max_alerts_per_day"] == 40
    assert out.siem_thresholds["splunk"] is None


def test_get_is_503_when_the_row_cannot_be_read(store, api):
    store.fail = True
    with pytest.raises(HTTPException) as e:
        routes.get_rollout_limits()
    assert e.value.status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"live_per_day": -1},
        {"live_per_day": 1001},
        {"live_per_day": "3"},
        {"live_per_day": 2.5},
        {"live_per_day": True},
        {"shadow_days": 0},
        {"shadow_days": 91},
        {"max_hits_per_day": 0},
        {"max_hits_per_day": 100001},
        {"shadows_days": 7},
        {"live_per_day": 1, "extra": 1},
    ],
)
def test_bad_values_and_unknown_keys_are_rejected_as_422(body):
    with pytest.raises(ValidationError):  # FastAPI answers a body error with 422
        routes.RolloutLimitsBody(**body)


def test_an_empty_edit_is_422(api):
    with pytest.raises(HTTPException) as e:
        put()
    assert e.value.status_code == 422


def test_a_change_needs_ai_autonomy_write_and_audits_who_old_and_new(api, store):
    out = put(live_per_day=2, reason="slower rollout")
    assert api.perms == ["ai_autonomy.write"]
    assert out.limits["live_per_day"] == 2
    (entry,) = api.audit
    assert entry["by"] == "u-ana"
    assert entry["config_key"] == "rollout.limits"
    assert entry["old_value"] == {
        "live_per_day": 5,
        "shadow_days": 7,
        "max_hits_per_day": 20,
    }
    assert entry["new_value"] == {
        "live_per_day": 2,
        "shadow_days": 7,
        "max_hits_per_day": 20,
    }
    assert "live_per_day 5 -> 2" in entry["change_reason"]
    assert "slower rollout" in entry["change_reason"]
    assert api.session.row.updated_by == "u-ana"  # the inserted row


def test_a_change_to_the_same_value_writes_no_audit_entry(api, store):
    put(live_per_day=5)
    assert api.audit == []


def test_a_service_account_may_tighten_but_not_loosen(api, store):
    store.value = {"live_per_day": 3, "shadow_days": 7, "max_hits_per_day": 20}
    for body in ({"live_per_day": 4}, {"shadow_days": 5}, {"max_hits_per_day": 21}):
        with pytest.raises(HTTPException) as e:
            put(SERVICE, **body)
        assert e.value.status_code == 403
    assert api.audit == []
    put(SERVICE, live_per_day=1, shadow_days=10, max_hits_per_day=10)
    assert api.audit[0]["by"] == "u-svc"


def test_the_new_value_reaches_the_next_sweep(api, store, monkeypatch):
    put(live_per_day=1)
    copies = [_copy(i) for i in (1, 2, 3)]
    assert len(_sweep(monkeypatch, copies)[0].moves) == 1


# --- audit row on a real Postgres ---------------------------------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")


@pytest.fixture
def db(monkeypatch):
    url = urlparse(DSN)
    monkeypatch.setenv("POSTGRES_HOST", url.hostname)
    monkeypatch.setenv("POSTGRES_PORT", str(url.port))
    monkeypatch.setenv("POSTGRES_DB", url.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", url.username)
    monkeypatch.setenv("POSTGRES_PASSWORD", url.password)
    monkeypatch.setenv("POSTGRES_SSL_MODE", "disable")
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    from sqlalchemy import text

    with manager.session_scope() as session:
        session.execute(text("DELETE FROM system_config WHERE key = 'rollout.limits'"))
        session.execute(
            text("DELETE FROM config_audit_log WHERE config_key = 'rollout.limits'")
        )
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@needs_pg
def test_a_change_stores_the_row_and_an_audit_row_with_actor_and_old_to_new(db):
    from sqlalchemy import select

    from core.storage.models import ConfigAuditLog

    rollout_limits.update_limits(
        user_id="u-ana",
        caller_is_service=False,
        changes={"shadow_days": 14},
        reason="slower",
    )
    assert rollout_limits.read_limits().shadow_days == 14
    rollout_limits.update_limits(
        user_id="u-ben", caller_is_service=False, changes={"live_per_day": 2}
    )
    assert rollout_limits.read_limits() == rollout_limits.RolloutLimits(2, 14, 20)
    with db.session_scope() as session:
        rows = (
            session.execute(
                select(ConfigAuditLog)
                .where(ConfigAuditLog.config_key == "rollout.limits")
                .order_by(ConfigAuditLog.id)
            )
            .scalars()
            .all()
        )
        got = [(r.changed_by, r.action, r.old_value, r.new_value) for r in rows]
    assert got[0][:2] == ("u-ana", "create") and got[0][3]["shadow_days"] == 14
    assert got[1][0] == "u-ben" and got[1][2]["live_per_day"] == 5
    assert got[1][3]["live_per_day"] == 2
