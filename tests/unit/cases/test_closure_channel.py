"""Trust follows the console sign-in, not a field the client can send (#3192)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.cases.agent_closure import (
    CALLER_UNAUTHENTICATED,
    activity_actor,
    actor_identity,
    bound_credential,
    clear_http_actor,
    close_channel,
    current_credential_id,
    note_http_actor,
    record_close,
)
from core.cases.closure import CHANNEL_AGENT, CHANNEL_COOKIE, trust_word
from core.integrations.mcp.surface import acting_as

pytestmark = pytest.mark.unit


def _note(*, via: str, person: bool = True, username: str = "nestor") -> None:
    note_http_actor(
        username=username,
        principal_id="u-nestor",
        via=via,
        person=person,
    )


def teardown_function() -> None:
    clear_http_actor()


def test_only_a_console_cookie_for_a_person_is_analyst():
    clear_http_actor()
    assert close_channel() == CHANNEL_AGENT
    assert trust_word(close_channel()) == "agent"

    _note(via="cookie")
    assert close_channel() == CHANNEL_COOKIE
    assert trust_word(close_channel()) == "analyst"

    _note(via="bearer")
    assert close_channel() == CHANNEL_AGENT

    _note(via="cookie", person=False)
    assert close_channel() == CHANNEL_AGENT


def test_an_unset_caller_stores_no_principal():
    clear_http_actor()
    assert actor_identity(session=object()) == (None, CALLER_UNAUTHENTICATED)


def test_a_live_username_becomes_a_principal_and_the_text_stays(monkeypatch):
    monkeypatch.setattr(
        "core.cases.agent_closure.principal_for_username",
        lambda session, name: "u-nestor" if name == "nestor" else None,
    )
    assert actor_identity(session=object(), explicit="nestor") == ("u-nestor", "nestor")
    assert actor_identity(session=object(), explicit="run-1") == (None, "run-1")
    assert actor_identity(session=object(), explicit="system") == (None, "system")


def test_a_new_activity_stores_the_principal_and_keeps_the_text(monkeypatch):
    monkeypatch.setattr(
        "core.cases.agent_closure.principal_for_username",
        lambda session, name: "u-nestor" if name == "nestor" else None,
    )
    assert activity_actor("nestor", session=object()) == {
        "actor": "u-nestor",
        "actor_text": "nestor",
    }
    clear_http_actor()
    assert activity_actor() == {"actor_text": "agent"}
    assert "actor" not in activity_actor()


def test_a_credential_is_carried_beside_the_username():
    assert current_credential_id() is None
    with acting_as("nestor"), bound_credential("mcpc-1"):
        assert current_credential_id() == "mcpc-1"
    assert current_credential_id() is None


def test_the_close_trail_names_the_principal_and_the_credential(monkeypatch):
    captured = {}

    def _append(session, payload, **beside):
        captured["payload"] = payload
        captured["beside"] = beside

    monkeypatch.setattr("core.cases.agent_closure._trail_ready", lambda session: True)
    monkeypatch.setattr("core.cases.agent_closure.trail.append", _append)
    record_close(
        MagicMock(),
        principal_id="u-nestor",
        case_id="case-1",
        actor_text="nestor",
        credential_id="mcpc-1",
    )
    assert captured["payload"] == {
        "action": "case.close",
        "case_id": "case-1",
        "actor": "u-nestor",
        "credential_id": "mcpc-1",
    }
    assert captured["beside"]["actor_text"] == "nestor"


def test_an_unset_close_omits_the_principal(monkeypatch):
    captured = {}
    monkeypatch.setattr("core.cases.agent_closure._trail_ready", lambda session: True)
    monkeypatch.setattr(
        "core.cases.agent_closure.trail.append",
        lambda session, payload, **beside: captured.update(payload=payload, beside=beside),
    )
    record_close(
        MagicMock(),
        principal_id=None,
        case_id="case-1",
        actor_text="agent",
    )
    assert "actor" not in captured["payload"]
    assert "credential_id" not in captured["payload"]
    assert captured["beside"]["actor_text"] == "agent"


@pytest.mark.asyncio
async def test_get_current_user_notes_the_door_it_accepted(monkeypatch):
    from core.auth import current_user
    from core.auth.auth_service import AuthService

    monkeypatch.setattr(current_user, "DEV_MODE", False)
    monkeypatch.setattr(current_user, "is_token_revoked", AsyncMock(return_value=False))
    monkeypatch.setattr(
        AuthService,
        "verify_jwt_token",
        staticmethod(lambda token: {"user_id": "u-1", "token_type": "access"}),
    )
    monkeypatch.setattr(
        AuthService, "verify_session_fingerprint", staticmethod(lambda *a, **k: True)
    )
    monkeypatch.setattr(current_user, "issued_before_cutoff", lambda *a, **k: False)
    user = SimpleNamespace(
        user_id="u-1",
        username="nestor",
        is_active=True,
        principal_id="u-1",
        service_account=False,
        tokens_valid_after=None,
    )
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user

    request = MagicMock()
    request.headers = {}
    request.cookies = {ACCESS_COOKIE_NAME: "cookie-token"}
    await current_user.get_current_user(
        request, "Bearer also-a-token", session=session
    )
    assert request.state.vigil_http_actor.via == "cookie"
    assert close_channel() == CHANNEL_COOKIE

    request.cookies = {}
    await current_user.get_current_user(
        request, "Bearer only-a-token", session=session
    )
    assert request.state.vigil_http_actor.via == "bearer"
    assert close_channel() == CHANNEL_AGENT


@pytest.mark.asyncio
async def test_dev_mode_is_not_a_cookie(monkeypatch):
    from core.auth import current_user

    monkeypatch.setattr(current_user, "DEV_MODE", True)
    monkeypatch.setattr(
        current_user, "_get_dev_user", lambda session: SimpleNamespace(username="admin")
    )
    _note(via="cookie")
    request = MagicMock()
    await current_user.get_current_user(request, None, session=MagicMock())
    assert close_channel() == CHANNEL_AGENT
    assert request.state.vigil_http_actor is None
