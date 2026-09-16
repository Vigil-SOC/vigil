"""A clean install can be signed into: bootstrap, log in, reach a closed route.

This is the path an operator walks the first time they open Vigil, and until
authentication became the default nothing exercised it — with the bypass on in
every development environment, the login screen was never reached, so "the
first admin can sign in" was an assumption rather than a fact.

The chain matters more than any link in it. Creating an account that cannot log
in, or logging in and receiving a token the next request rejects, both leave an
install nobody can use, and both look fine if you only test one step.

Runs against a real database with an empty ``users`` table, because both halves
of the behaviour under test are database state: bootstrap is offered only while
no account exists, and closes permanently once one does.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database,
    pytest.mark.external_service,
]

REPO_ROOT = Path(__file__).resolve().parents[2]

# Roles and users, with the admin role the bootstrap account is given. Python
# models these, but the role rows are seeded by SQL, and a bootstrap without
# role-admin present creates an account holding no permissions.
AUTH_DDL = ("06_auth_tables.sql",)

PASSWORD = "an-operators-own-choice-8ZQ2"


def _database_url() -> str:
    return os.environ.get(
        "AUTH_TEST_DATABASE_URL",
        "postgresql://deeptempo:deeptempo_secure_password_change_me"
        "@127.0.0.1:5432/vigil_auth_test",
    )


@pytest.fixture(scope="module")
def empty_install():
    """A database with the auth schema, the seeded roles, and no accounts."""
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        # Applied once. The file creates triggers, which have no IF NOT EXISTS,
        # so re-running it against a database that already has the schema is an
        # error rather than a no-op.
        already = conn.execute(
            text("SELECT to_regclass('public.users') IS NOT NULL")
        ).scalar()
        if not already:
            for name in AUTH_DDL:
                conn.execute(
                    text((REPO_ROOT / "infra" / "database" / "init" / name).read_text())
                )
            conn.commit()
        # The state this is about: no account exists yet.
        conn.execute(text("TRUNCATE users CASCADE"))
        conn.commit()
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def client(empty_install, monkeypatch_module, tmp_path_factory):
    """The API, talking to that database, with authentication on."""
    # DATABASE_URL is the TypeScript agent's variable; the Python side reads the
    # encrypted store's DSN and falls back to POSTGRES_*. Setting the former and
    # assuming it took is how this test first ran against the developer's own
    # database. Point VIGIL_DIR at an empty directory so no stored DSN wins, and
    # set the variables that are actually consulted.
    url = make_url(_database_url())
    monkeypatch_module.setenv("VIGIL_DIR", str(tmp_path_factory.mktemp("vigil-home")))
    monkeypatch_module.setenv("POSTGRES_HOST", url.host or "127.0.0.1")
    monkeypatch_module.setenv("POSTGRES_PORT", str(url.port or 5432))
    monkeypatch_module.setenv("POSTGRES_DB", url.database or "")
    monkeypatch_module.setenv("POSTGRES_USER", url.username or "")
    monkeypatch_module.setenv("POSTGRES_PASSWORD", url.password or "")
    monkeypatch_module.setenv("DEV_MODE", "false")
    monkeypatch_module.setenv("VIGIL_CSRF_ENABLED", "false")
    # Auth cookies ship Secure, and TestClient speaks http://testserver, which a
    # Secure cookie is never sent back over. Browsers exempt localhost, so this
    # is the harness and not the product — but the cookie leg has to be exercised
    # somewhere, and this is the only place that walks the whole chain.
    monkeypatch_module.setenv("VIGIL_COOKIE_SECURE", "false")
    monkeypatch_module.setenv("TESTING", "true")

    from core.config import get_settings

    get_settings.cache_clear()

    # The manager is a process-wide singleton built on first use. Under the
    # whole integration suite an earlier module has already asked for one, so
    # it holds CI's job-wide POSTGRES_DB and not the variables set above —
    # which is why this passes run alone and fails in CI. Drop it here, and
    # again on the way out so the next module builds its own.
    from core.storage.connection import get_db_manager, reset_db_manager

    reset_db_manager()

    # Refuse to run against anything but the database this test was handed.
    # These tests TRUNCATE users; pointed at a real install that is destructive,
    # and the failure mode is silent because the API answers perfectly well.
    resolved = make_url(get_db_manager().config.get_database_url())
    assert resolved.database == url.database, (
        f"refusing to run: the API resolved database {resolved.database!r}, "
        f"not the test database {url.database!r}"
    )

    from fastapi.testclient import TestClient

    from services.api.main import app

    with TestClient(app) as c:
        yield c

    reset_db_manager()


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


def test_a_clean_install_offers_to_create_the_first_admin(client):
    response = client.get("/api/auth/bootstrap")

    assert response.status_code == 200, response.text
    assert response.json()["required"] is True


def test_the_chain_from_no_account_to_an_authenticated_call(client):
    username = f"operator-{uuid.uuid4().hex[:8]}"

    created = client.post(
        "/api/auth/bootstrap",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": PASSWORD,
            "full_name": "First Operator",
        },
    )
    assert created.status_code == 201, created.text

    signed_in = client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": PASSWORD},
    )
    assert signed_in.status_code == 200, signed_in.text
    token = signed_in.json()["access_token"]
    assert token

    # The link that makes the other two worth anything: the credential the login
    # returned is accepted by a route that requires one.
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["username"] == username

    # And the cookie the login set works without the header, which is what the
    # console actually uses.
    with_cookie = client.get("/api/auth/me")
    assert with_cookie.status_code == 200, with_cookie.text


def test_bootstrap_closes_once_an_account_exists(client):
    """Not a signup endpoint: it is unauthenticated and must shut permanently."""
    assert client.get("/api/auth/bootstrap").json()["required"] is False

    second = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "a-second-admin",
            "email": "second@example.com",
            "password": PASSWORD,
            "full_name": "Second",
        },
    )
    assert second.status_code == 403, second.text


def test_a_closed_route_refuses_an_unauthenticated_caller(client):
    """The other half of the default: without a credential, nothing opens."""
    client.cookies.clear()

    assert client.get("/api/auth/me").status_code == 401
