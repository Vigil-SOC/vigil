"""
Unit tests for backend.services.password_validator.

Covers length, character-class, and common-password blocklist rules.
Uses explicit override arguments so the tests don't depend on the bundled
blocklist file staying exactly as it is today.
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
            "V1gilStrong2026!", blocklist=SAMPLE_BLOCKLIST, min_length=12
        )

    def test_rejects_short_password(self):
        with pytest.raises(PasswordPolicyError, match="at least 12"):
            validate_password_strength(
                "A1bc", blocklist=SAMPLE_BLOCKLIST, min_length=12
            )

    def test_rejects_password_without_letters(self):
        with pytest.raises(PasswordPolicyError, match="at least one letter"):
            validate_password_strength(
                "1234567890123", blocklist=SAMPLE_BLOCKLIST, min_length=12
            )

    def test_rejects_password_without_digits(self):
        with pytest.raises(PasswordPolicyError, match="at least one digit"):
            validate_password_strength(
                "JustLettersHere", blocklist=SAMPLE_BLOCKLIST, min_length=12
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

    def test_custom_min_length_is_respected(self):
        # Too short for a stricter policy
        with pytest.raises(PasswordPolicyError):
            validate_password_strength(
                "abc123def", blocklist=SAMPLE_BLOCKLIST, min_length=16
            )
        # But OK for a looser one
        validate_password_strength(
            "abc123def", blocklist=SAMPLE_BLOCKLIST, min_length=8
        )

    def test_bundled_blocklist_actually_rejects_known_bad(self):
        """Smoke-test the bundled data/common_passwords.txt file — if
        someone renames or moves it, this surfaces the regression."""
        with pytest.raises(PasswordPolicyError, match="too common"):
            # "Password1!" meets 12-char-ish policies in many systems but
            # is in every breach corpus — the blocklist must catch it.
            validate_password_strength("Password1!", min_length=8)
