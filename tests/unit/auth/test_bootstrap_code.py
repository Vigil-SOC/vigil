"""The first-run setup code is a pure function of the JWT signing key."""

import logging
from contextlib import contextmanager

import pytest

from core.auth import bootstrap_code
from core.auth.password_reset import RESET_TOKEN_PURPOSE


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


def test_announce_logs_the_code_only_while_users_is_empty(caplog, monkeypatch):
    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-one", raising=False
    )

    class _Query:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _Session:
        def __init__(self, row):
            self._row = row

        def query(self, *_args, **_kwargs):
            return _Query(self._row)

    def install(row):
        @contextmanager
        def uow():
            yield _Session(row)

        monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", uow)

    install(None)
    with caplog.at_level(logging.WARNING):
        bootstrap_code.announce_setup_code()
    messages = [r.getMessage() for r in caplog.records]
    code = bootstrap_code.setup_code()
    assert any(m == f"First-run setup code: {code}" for m in messages)
    # The template recorded for grouping does not carry the value.
    logged = [r for r in caplog.records if code in r.getMessage()]
    assert logged
    assert all(r.msg == bootstrap_code.SETUP_CODE_LOG_TEMPLATE for r in logged)

    caplog.clear()
    install(("user-1",))
    with caplog.at_level(logging.WARNING):
        bootstrap_code.announce_setup_code()
    assert not any(bootstrap_code.SETUP_CODE_LOG_MARK in r.getMessage() for r in caplog.records)


def test_announce_skips_the_code_when_the_query_fails(caplog, monkeypatch):
    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", boom)
    with caplog.at_level(logging.WARNING):
        bootstrap_code.announce_setup_code()
    assert not any(bootstrap_code.SETUP_CODE_LOG_MARK in r.getMessage() for r in caplog.records)
    assert any("database down" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("submitted", [None, "", "   ", "nope"])
def test_mismatch_is_false(submitted, monkeypatch):
    monkeypatch.setattr(
        "core.auth.auth_service.JWT_SECRET_KEY", "test-secret-one", raising=False
    )
    assert bootstrap_code.setup_code_matches(submitted) is False
