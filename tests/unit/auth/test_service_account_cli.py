"""``python -m core.auth.service_account ensure``: the enable flow's account step.

Every install shape runs it inside the backend's environment with the username as
an argument and the password on stdin (never argv or env: ``docker exec`` events
carry the full command line). It creates Medic's gateway login as a Viewer
service account, is idempotent, rotates the password when handed a new one, and
refuses to touch a person's account. It never prints the password.
"""

import io
import re

import pytest
from sqlalchemy import text

from core.auth import service_account as sa
from core.auth.auth_service import AuthService
from core.storage.connection import get_db_manager
from core.storage.models import Role, User
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

NAME = "medic-a1b2c3d4e5f6"
PASSWORD = (
    "a" * 32 + "0123456789abcdef0123456789abcdef"
)  # 64 chars, as the scripts mint


@pytest.fixture(autouse=True)
def _clean_users():
    """Viewer role present (db-seed's job in a real install), no users."""
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


def _run(*argv: str, stdin: str = PASSWORD + "\n"):
    out, err = io.StringIO(), io.StringIO()
    code = sa.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _user(username: str = NAME) -> User:
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == username).one()
        session.expunge(user)
        return user


def test_creates_a_viewer_service_account():
    code, out, err = _run("ensure", NAME)

    assert code == 0, err
    assert out.strip() == f"service account {NAME}: created"
    user = _user()
    assert user.service_account is True
    assert user.role_id == "role-viewer"
    assert user.is_active is True
    assert AuthService.verify_password(PASSWORD, user.password_hash)


def test_rerun_with_the_same_password_changes_nothing():
    _run("ensure", NAME)
    before = _user().password_hash

    code, out, _ = _run("ensure", NAME)

    assert code == 0
    assert out.strip() == f"service account {NAME}: unchanged"
    assert _user().password_hash == before


def test_a_new_password_rotates_and_clears_failures():
    _run("ensure", NAME)
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == NAME).one()
        user.failed_login_count = 40
    new = "b" * 64

    code, out, _ = _run("ensure", NAME, stdin=new + "\n")

    assert code == 0
    assert out.strip() == f"service account {NAME}: rotated"
    user = _user()
    assert AuthService.verify_password(new, user.password_hash)
    assert not AuthService.verify_password(PASSWORD, user.password_hash)
    assert user.failed_login_count == 0


def test_a_new_name_retires_the_old_service_account():
    """A wiped secrets dir mints a new name; the old login must not stay usable."""
    old = "medic-000000000000"
    _run("ensure", old)

    code, out, _ = _run("ensure", NAME)

    assert code == 0
    assert "retired 1" in out
    assert _user(old).is_active is False
    assert _user(NAME).is_active is True


def test_refuses_a_persons_account_with_that_name():
    AuthService.create_user(
        username=NAME,
        email="person@example.test",
        password="Correct-Horse-Battery-9",
        full_name="A Person",
        role_id="role-viewer",
    )
    before = _user().password_hash

    code, out, err = _run("ensure", NAME)

    assert code == sa.EX_REFUSED == 77
    assert "not a service account" in err
    assert _user().service_account is False
    assert _user().password_hash == before


def test_refuses_a_service_account_that_was_given_another_role():
    _run("ensure", NAME)
    with unit_of_work() as session:
        if not session.query(Role).filter(Role.role_id == "role-admin").first():
            session.add(
                Role(
                    role_id="role-admin",
                    name="Admin",
                    description="All",
                    permissions={"admin": True},
                    is_system_role=True,
                )
            )
        session.query(User).filter(User.username == NAME).one().role_id = "role-admin"

    code, _, err = _run("ensure", NAME)

    assert code == 77
    assert "Viewer" in err


@pytest.mark.parametrize(
    "name",
    [
        "medic-viewer",
        "medic-ABCDEFGHIJKL",
        "medic-a1b2c3d4e5f",
        "admin",
        "medic-a1b2c3d4e5f6x",
    ],
)
def test_refuses_a_guessable_or_malformed_name(name):
    code, _, err = _run("ensure", name)

    assert code == sa.EX_USAGE == 64
    assert re.search(r"medic-\[a-z0-9\]\{12\}", err)


@pytest.mark.parametrize(
    "password",
    ["", "short", "x" * 31, "x" * 73, "é" * 40, "has space" * 5, "x" * 40 + "\x00"],
)
def test_refuses_a_weak_or_unusable_password(password):
    code, out, err = _run("ensure", NAME, stdin=password + "\n")

    assert code == 64
    assert password.strip() == "" or password not in out + err
    with unit_of_work() as session:
        assert session.query(User).count() == 0


def test_never_takes_the_password_from_argv():
    code, _, _ = _run("ensure", NAME, PASSWORD)
    assert code == 64


def test_waits_for_the_schema_with_a_tempfail_code(monkeypatch):
    """No column yet (an upgrade before db-seed ran): 75, so the script retries."""
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text("ALTER TABLE users DROP COLUMN service_account"))
            assert sa.schema_ready(conn) is False
        finally:
            tx.rollback()

    monkeypatch.setattr(sa, "schema_ready", lambda conn: False)
    code, _, err = _run("ensure", NAME)
    assert code == sa.EX_TEMPFAIL == 75
    assert "not ready" in err


def test_waits_for_the_viewer_role():
    with unit_of_work() as session:
        session.query(Role).filter(Role.role_id == "role-viewer").delete()

    code, _, err = _run("ensure", NAME)

    assert code == 75
    assert "role-viewer" in err


def test_output_never_holds_the_password(caplog):
    caplog.set_level("DEBUG")
    for stdin in (PASSWORD + "\n", "c" * 64 + "\n"):
        _, out, err = _run("ensure", NAME, stdin=stdin)
        secret = stdin.strip()
        assert secret not in out
        assert secret not in err
        assert secret not in caplog.text


def test_a_fresh_process_initialises_the_database(monkeypatch):
    """`python -m` starts with no engine; the live run found this, not the fixture."""
    manager = get_db_manager()
    calls = []
    real_engine = manager.engine

    class Fresh:
        engine = None

        def initialize(self):
            calls.append("init")
            self.engine = real_engine

    fresh = Fresh()
    monkeypatch.setattr(sa, "get_db_manager", lambda: fresh)

    code, _, err = _run("ensure", NAME)

    assert calls == ["init"]
    assert code == 0, err


def test_a_service_account_does_not_close_first_admin_bootstrap():
    """Enabling Medic before anyone signs in must leave the install claimable."""
    from services.api.routers import auth as auth_router

    _run("ensure", NAME)

    with unit_of_work() as session:
        assert auth_router._has_any_user(session) is False
        assert auth_router.bootstrap_status(session).required is True

    AuthService.create_user(
        username="first-admin",
        email="admin@example.com",
        password="Correct-Horse-Battery-9",
        full_name="Admin",
        role_id="role-viewer",
    )
    with unit_of_work() as session:
        assert auth_router._has_any_user(session) is True


def test_ensure_undoes_what_a_stolen_session_could_change():
    """Rotation must recover the account: email back, MFA off (review S5)."""
    _run("ensure", NAME)
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == NAME).one()
        user.email = "attacker@example.com"
        user.mfa_enabled = True
        user.mfa_secret = "gAAAAA-something"
        user.mfa_recovery_codes = ["x"]

    code, _, err = _run("ensure", NAME, stdin="d" * 64 + "\n")

    assert code == 0, err
    user = _user()
    assert user.email == f"{NAME}@service.invalid"
    assert user.mfa_enabled is False
    assert user.mfa_secret is None
    assert user.mfa_recovery_codes == []


def test_a_database_error_prints_no_parameters(monkeypatch):
    """SQLAlchemy errors carry the statement's parameters (the hash): type only."""
    from sqlalchemy.exc import IntegrityError

    def boom(*a, **k):
        raise IntegrityError("INSERT ...", {"password_hash": "$2b$SECRETHASH"}, None)

    monkeypatch.setattr(sa, "ensure", boom)
    code, out, err = _run("ensure", NAME)
    assert code == sa.EX_SOFTWARE == 70
    assert "SECRETHASH" not in out + err
    assert "IntegrityError" in err


def test_no_users_table_yet_is_not_ready_not_an_error(monkeypatch):
    from sqlalchemy.exc import NoSuchTableError

    def missing(conn):
        raise NoSuchTableError("users")

    monkeypatch.setattr(
        sa,
        "inspect",
        lambda conn: type("I", (), {"get_columns": staticmethod(missing)})(),
    )
    code, _, err = _run("ensure", NAME)
    assert code == 75, err
