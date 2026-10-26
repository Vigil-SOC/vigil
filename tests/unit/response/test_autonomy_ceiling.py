"""The autonomy ceiling: env floor, stop and resume, service accounts (#2918)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from core.response.ceiling import (
    CEILING_KEY,
    Ceiling,
    CeilingRefusal,
    effective_act_irreversible,
    effective_act_reversible,
    execution_held,
    resume_ceiling,
    stop_ceiling,
    update_ceiling,
)
from core.upgrade.command import PACKAGE_DIR
from services.api.routers.config import (
    ForceManualApprovalConfig,
    set_force_manual_approval,
)

pytestmark = pytest.mark.unit

_OFF = {
    "act_reversible": False,
    "act_irreversible": False,
    "stopped": False,
    "four_eyes": False,
}


class _Store:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def read_system_config(self, key):
        return self.rows.get(key)

    def set_system_config(self, key, value, **_kwargs):
        self.rows[key] = value
        return True


@pytest.fixture
def store(monkeypatch):
    svc = _Store()
    monkeypatch.setattr(
        "core.storage.config_service.get_config_service", lambda **_k: svc
    )
    return svc


def test_revision_chains_off_the_origin_run_drop():
    path = PACKAGE_DIR / "versions" / "0010_autonomy_ceiling.py"
    spec = importlib.util.spec_from_file_location("autonomy_ceiling_0010", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    sql = Path(revision.__file__).with_suffix(".sql").read_text(encoding="utf-8")

    assert revision.revision == "0010_autonomy_ceiling"
    assert revision.down_revision == "0009_drop_origin_run_ids"
    assert isinstance(revision.down_revision, str)
    assert "autonomy.ceiling" in sql
    assert '"act_reversible": false' in sql
    assert '"act_irreversible": false' in sql
    assert '"four_eyes": false' in sql
    assert (
        "DELETE FROM system_config WHERE key = 'approval.force_manual_approval'" in sql
    )
    assert "approval_actions" not in sql
    assert "tool_effect" not in sql


def test_env_cannot_turn_an_act_flag_on():
    stored = Ceiling(act_reversible=True, act_irreversible=True)
    locked = type(
        "S", (), {"daemon_force_approval": False, "daemon_auto_response": False}
    )()
    forced = type(
        "S", (), {"daemon_force_approval": True, "daemon_auto_response": True}
    )()
    open_env = type(
        "S", (), {"daemon_force_approval": False, "daemon_auto_response": True}
    )()

    assert effective_act_reversible(stored, locked) is False
    assert effective_act_irreversible(stored, forced) is False
    assert effective_act_reversible(stored, open_env) is True
    assert effective_act_reversible(Ceiling(), open_env) is False


def test_a_locked_environment_does_not_store_an_act_flag(store):
    with pytest.raises(CeilingRefusal) as exc:
        update_ceiling(
            user_id="u",
            caller_is_service=False,
            act_reversible=True,
            locked=True,
        )
    assert exc.value.status_code == 409
    assert CEILING_KEY not in store.rows


def test_service_account_can_lower_and_cannot_raise_resume_or_switch(store):
    store.rows[CEILING_KEY] = {**_OFF, "act_reversible": True, "four_eyes": False}

    lowered = update_ceiling(
        user_id="sa",
        caller_is_service=True,
        act_reversible=False,
        locked=False,
    )
    assert lowered.act_reversible is False

    store.rows[CEILING_KEY] = {**_OFF, "act_reversible": False}
    with pytest.raises(CeilingRefusal) as raised:
        update_ceiling(
            user_id="sa",
            caller_is_service=True,
            act_reversible=True,
            locked=False,
        )
    assert raised.value.status_code == 403

    with pytest.raises(CeilingRefusal) as eyes:
        update_ceiling(
            user_id="sa",
            caller_is_service=True,
            four_eyes=True,
            locked=False,
        )
    assert eyes.value.status_code == 403

    store.rows[CEILING_KEY] = {**_OFF, "stopped": True, "act_reversible": True}
    with pytest.raises(CeilingRefusal) as resumed:
        resume_ceiling(user_id="sa", caller_is_service=True)
    assert resumed.value.status_code == 403
    assert store.rows[CEILING_KEY]["stopped"] is True
    assert store.rows[CEILING_KEY]["act_reversible"] is True


def test_stop_then_lower_then_resume_keeps_the_lower(store):
    store.rows[CEILING_KEY] = {
        **_OFF,
        "act_reversible": True,
        "act_irreversible": True,
    }
    stopped = stop_ceiling("u")
    assert stopped.stopped is True
    assert stopped.act_reversible is True
    assert execution_held() is True

    update_ceiling(
        user_id="u",
        caller_is_service=False,
        act_irreversible=False,
        locked=False,
    )
    restored = resume_ceiling(user_id="u", caller_is_service=False)
    assert restored.stopped is False
    assert restored.act_reversible is True
    assert restored.act_irreversible is False
    assert execution_held() is False


def test_compat_act_post_refuses_a_service_account(store, monkeypatch):
    monkeypatch.setattr(
        "services.api.routers.config.get_settings",
        lambda: type(
            "S", (), {"daemon_force_approval": False, "daemon_auto_response": True}
        )(),
    )
    user = type("U", (), {"user_id": "sa", "service_account": True})()
    with pytest.raises(HTTPException) as exc:
        set_force_manual_approval(ForceManualApprovalConfig(enabled=False), user)
    assert exc.value.status_code == 403
    assert CEILING_KEY not in store.rows

    person = type("U", (), {"user_id": "p", "service_account": False})()
    assist = set_force_manual_approval(ForceManualApprovalConfig(enabled=True), person)
    assert assist.enabled is True
