"""
Unit tests for backend.services.password_validator (zxcvbn-backed).

Covers length, blocklist, entropy scoring, and user_inputs penalization.
"""

import pytest

from backend.services.password_validator import (
    PasswordPolicyError,
    validate_password_strength,
)


SAMPLE_BLOCKLIST = frozenset({"password123", "letmein123456", "correcthorse42"})


class TestPasswordValidator:
    def test_accepts_strong_password(self):
        validate_password_strength(
            "V1gilStr0ng!2026xQ", blocklist=SAMPLE_BLOCKLIST, min_length=12
        )

    def test_rejects_short_password(self):
        with pytest.raises(PasswordPolicyError, match="at least 12"):
            validate_password_strength(
                "A1bc", blocklist=SAMPLE_BLOCKLIST, min_length=12
            )

    def test_rejects_blocklisted_password(self):
        with pytest.raises(PasswordPolicyError, match="too common"):
            validate_password_strength(
                "Password123", blocklist=SAMPLE_BLOCKLIST, min_length=8
            )

    def test_blocklist_is_case_insensitive(self):
        with pytest.raises(PasswordPolicyError, match="too common"):
            validate_password_strength(
                "PASSWORD123", blocklist=SAMPLE_BLOCKLIST, min_length=8
            )

    def test_rejects_weak_entropy_password(self):
        with pytest.raises(PasswordPolicyError, match="too weak"):
            validate_password_strength(
                "aaaaaaaaaaaa1", blocklist=SAMPLE_BLOCKLIST, min_length=8
            )

    def test_rejects_common_pattern(self):
        with pytest.raises(PasswordPolicyError, match="too weak"):
            validate_password_strength(
                "qwerty12345678", blocklist=SAMPLE_BLOCKLIST, min_length=8
            )

    def test_custom_min_length_is_respected(self):
        with pytest.raises(PasswordPolicyError):
            validate_password_strength(
                "abc123def", blocklist=SAMPLE_BLOCKLIST, min_length=16
            )

    def test_user_inputs_penalized(self):
        # A password derived from the username should score lower
        with pytest.raises(PasswordPolicyError, match="too weak"):
            validate_password_strength(
                "matthewmorris1",
                blocklist=SAMPLE_BLOCKLIST,
                min_length=8,
                user_inputs=["matthewmorris"],
            )

    def test_suggestions_returned_on_failure(self):
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_strength(
                "password1234", blocklist=frozenset(), min_length=8
            )
        assert len(exc_info.value.suggestions) > 0

    def test_min_score_override(self):
        # score=2 password should pass with min_score=2 but fail at 3
        weak_but_ok = "sunflower42z"
        validate_password_strength(
            weak_but_ok, blocklist=SAMPLE_BLOCKLIST, min_length=8, min_score=1
        )

    def test_bundled_blocklist_rejects_known_bad(self):
        """Smoke-test the bundled data/common_passwords.txt file."""
        with pytest.raises(PasswordPolicyError, match="too common"):
            validate_password_strength("Password1!", min_length=8)
