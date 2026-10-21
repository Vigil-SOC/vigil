"""A recovery code is accepted once even when two requests carry it at the
same moment (#2575): verify_mfa_code locks the user row, so the second
request reads the list the first one committed.

Runs against a scratch database on a local Postgres; skips without one,
except under ``CI``.
"""

from __future__ import annotations

import os
import threading
import time

import bcrypt
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import core.auth.auth_service as auth_service
from core.auth.auth_service import AuthService
from core.storage.models import Base, Role, User

pytestmark = [pytest.mark.integration, pytest.mark.database]

SCRATCH_DB = "vigil_test_mfa_single_use"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"}  # nosec B104
_CONNECT_ARGS = {"connect_timeout": 5}
RECOVERY_CODE = "ABCD1234"


def _url(database: str) -> str:
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture(scope="module")
def engine():
    host = os.getenv("POSTGRES_HOST", "localhost")
    admin = create_engine(
        _url("postgres"), isolation_level="AUTOCOMMIT", connect_args=_CONNECT_ARGS
    )
    try:
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(f"refusing to CREATE/DROP on non-local host {host!r}")
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
            c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    except Exception as e:  # noqa: BLE001
        admin.dispose()
        if os.getenv("CI"):
            pytest.fail(f"CI must run the recovery-code race test: {e}")
        pytest.skip(f"requires a local PostgreSQL: {e}")

    scratch = create_engine(_url(SCRATCH_DB), connect_args=_CONNECT_ARGS)
    with scratch.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        c.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    Base.metadata.create_all(scratch)
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


def test_concurrent_recovery_code_is_accepted_exactly_once(engine, monkeypatch):
    # Hold each check open long enough that, without the row lock, both
    # requests would read the full list before either commits.
    checkpw = bcrypt.checkpw

    def slow_checkpw(*args):
        time.sleep(0.5)
        return checkpw(*args)

    monkeypatch.setattr(auth_service.bcrypt, "checkpw", slow_checkpw)
    hashes = [
        bcrypt.hashpw(c.encode(), bcrypt.gensalt(4)).decode()
        for c in (RECOVERY_CODE, "EEEE0000")
    ]
    with Session(engine) as s:
        s.add(
            Role(role_id="role-viewer", name="Viewer", description="", permissions={})
        )
        s.add(
            User(
                user_id="u-race",
                username="race",
                email="race@example.com",
                password_hash="x",
                full_name="Race",
                role_id="role-viewer",
                mfa_enabled=True,
                mfa_secret=AuthService._encrypt_mfa_secret("JBSWY3DPEHPK3PXP"),
                mfa_recovery_codes=hashes,
            )
        )
        s.commit()

    barrier = threading.Barrier(2)
    results = []

    def attempt():
        # Each thread is its own request: own session, commit at the end.
        with Session(engine) as s:
            barrier.wait()
            ok = AuthService.verify_mfa_code("u-race", RECOVERY_CODE, s)
            s.commit()
            results.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(results) == [False, True]
    with Session(engine) as s:
        assert s.get(User, "u-race").mfa_recovery_codes == hashes[1:]
