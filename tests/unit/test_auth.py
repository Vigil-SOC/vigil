"""
Unit tests for authentication building blocks.

These tests replace tests/broken_tests/test_auth.py, which was pinned to
an old AuthService API (create_access_token / decode_token / has_permission)
that no longer exists. The tests here match the current methods:
generate_jwt_token / verify_jwt_token / check_permission.

DB-touching flows (authenticate_user, check_permission) are covered by
integration tests; here we stay with the pure/static pieces.
"""

import os

# Pin a deterministic JWT secret before importing auth_service so the
# module's import-time secret loader uses it. Without this, running the
# test suite without JWT_SECRET_KEY in DEV_MODE=false would fail.
os.environ.setdefault("DEV_MODE", "true")

import jwt
import pytest

from backend.services.auth_service import (
    AuthService,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    password_matches_any,
)


# ----- Password hashing -----

class TestPasswordHashing:
    def test_hash_produces_bcrypt_hash(self):
        hashed = AuthService.hash_password("SecurePassword123!")
        assert hashed.startswith("$2b$")
        assert len(hashed) == 60

    def test_verify_password_correct(self):
        pw = "SecurePassword123!"
        assert AuthService.verify_password(pw, AuthService.hash_password(pw)) is True

    def test_verify_password_wrong(self):
        hashed = AuthService.hash_password("SecurePassword123!")
        assert AuthService.verify_password("WrongPassword!", hashed) is False

    def test_hash_has_unique_salt(self):
        pw = "SecurePassword123!"
        assert AuthService.hash_password(pw) != AuthService.hash_password(pw)


# ----- JWT -----

class _FakeUser:
    user_id = "user-abc"
    username = "alice"
    email = "alice@example.com"
    role_id = "role-analyst"


class TestJWT:
    def test_token_is_three_part_jwt(self):
        token = AuthService.generate_jwt_token(_FakeUser(), "access")
        assert len(token.split(".")) == 3

    def test_token_contains_standard_claims(self):
        token = AuthService.generate_jwt_token(_FakeUser(), "access")
        decoded = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        assert decoded["user_id"] == "user-abc"
        assert decoded["username"] == "alice"
        assert decoded["role_id"] == "role-analyst"
        assert decoded["token_type"] == "access"
        assert "jti" in decoded  # Added in PR 3 for revocation
        assert "iat" in decoded
        assert "exp" in decoded

    def test_verify_accepts_freshly_issued_token(self):
        token = AuthService.generate_jwt_token(_FakeUser(), "access")
        assert AuthService.verify_jwt_token(token) is not None

    def test_verify_rejects_garbage(self):
        assert AuthService.verify_jwt_token("not.a.valid.jwt") is None

    def test_verify_rejects_wrong_signature(self):
        # Sign with a different secret and ensure our verify rejects it.
        token = jwt.encode(
            {"user_id": "x", "exp": 9999999999},
            "some-other-secret",
            algorithm=JWT_ALGORITHM,
        )
        assert AuthService.verify_jwt_token(token) is None

    def test_access_and_refresh_have_distinct_jti(self):
        access = AuthService.generate_jwt_token(_FakeUser(), "access")
        refresh = AuthService.generate_jwt_token(_FakeUser(), "refresh")
        a = jwt.decode(access, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        r = jwt.decode(refresh, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        assert a["jti"] != r["jti"]
        assert a["token_type"] == "access"
        assert r["token_type"] == "refresh"


# ----- Password history helper -----

class TestPasswordHistoryHelper:
    def test_empty_history_matches_nothing(self):
        assert password_matches_any("hello", []) is False
        assert password_matches_any("hello", None) is False

    def test_matches_known_hash(self):
        hashed = AuthService.hash_password("s3cret-value!")
        assert password_matches_any("s3cret-value!", [hashed]) is True

    def test_does_not_match_other_password(self):
        hashed = AuthService.hash_password("s3cret-value!")
        assert password_matches_any("different-password", [hashed]) is False

    def test_ignores_garbage_entries(self):
        good = AuthService.hash_password("s3cret-value!")
        # Garbage in the list must not explode the compare
        assert password_matches_any("s3cret-value!", ["", "garbage", good]) is True
