"""The first-run setup code is a pure function of the JWT signing key."""

import io
import logging

import pytest

from core.auth import bootstrap_code, service_account
from core.auth.auth_service import AuthService
from core.auth.password_reset import RESET_TOKEN_PURPOSE
from core.storage.models import Role, User
from core.storage.unit_of_work import unit_of_work


def test_code_is_stable_and_not_the_password_reset_purpose(monkeypatch):
    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-one", raising=False
    )
    first = bootstrap_code.setup_code()
    assert first == bootstrap_code.setup_code()
    assert len(first) == 32
    assert bootstrap_code.SETUP_CODE_PURPOSE != RESET_TOKEN_PURPOSE
    assert bootstrap_code.setup_code_matches(f"  {first}  ")
    assert not bootstrap_code.setup_code_matches(None)
    assert not bootstrap_code.setup_code_matches("")
    assert not bootstrap_code.setup_code_matches("wrong-code")
    assert not bootstrap_code.setup_code_matches("0" * len(first))

    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-two", raising=False
    )
    assert bootstrap_code.setup_code() != first


@pytest.fixture
def _no_users():
    with unit_of_work() as session:
        session.query(User).delete()
        if not session.query(Role).filter(Role.role_id == "role-viewer").first():
            session.add(
                Role(
                    role_id="role-viewer",
                    name="Viewer",
                    description="Read-only",
                    permissions={"findings.read": True},
                    is_system_role=True,
                )
            )
    yield
    with unit_of_work() as session:
        session.query(User).delete()


def _announced(caplog) -> list[logging.LogRecord]:
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        bootstrap_code.announce_setup_code()
    return [
        r
        for r in caplog.records
        if bootstrap_code.SETUP_CODE_LOG_MARK in r.getMessage()
    ]


@pytest.mark.external_service
@pytest.mark.database
def test_announce_logs_the_code_only_while_no_human_user_exists(
    _no_users, caplog, monkeypatch
):
    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-one", raising=False
    )
    code = bootstrap_code.setup_code()

    logged = _announced(caplog)
    assert [r.getMessage() for r in logged] == [f"First-run setup code: {code}"]
    # The template recorded for grouping does not carry the value.
    assert all(r.msg == bootstrap_code.SETUP_CODE_LOG_TEMPLATE for r in logged)

    exit_code = service_account.main(
        ["ensure", "medic-a1b2c3d4e5f6"],
        stdin=io.StringIO("d" * 64 + "\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert exit_code == 0
    assert [r.getMessage() for r in _announced(caplog)] == [
        f"First-run setup code: {code}"
    ]

    AuthService.create_user(
        username="first-admin",
        email="admin@example.com",
        password="Correct-Horse-Battery-9",
        full_name="Admin",
        role_id="role-viewer",
    )
    assert _announced(caplog) == []


def test_announce_skips_the_code_when_the_query_fails(caplog, monkeypatch):
    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", boom)
    with caplog.at_level(logging.WARNING):
        bootstrap_code.announce_setup_code()
    assert not any(
        bootstrap_code.SETUP_CODE_LOG_MARK in r.getMessage() for r in caplog.records
    )
    assert any("database down" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("submitted", [None, "", "   ", "nope"])
def test_mismatch_is_false(submitted, monkeypatch):
    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-one", raising=False
    )
    assert bootstrap_code.setup_code_matches(submitted) is False
