"""Password strength validation using zxcvbn entropy scoring (NIST 800-63B)."""

import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional

from zxcvbn import zxcvbn

logger = logging.getLogger(__name__)

_MIN_LENGTH = 12
_MIN_SCORE = 3
_BLOCKLIST_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "common_passwords.txt"
_blocklist: Optional[frozenset] = None


class PasswordPolicyError(ValueError):
    def __init__(self, message: str, *, suggestions: Optional[List[str]] = None):
        super().__init__(message)
        self.suggestions = suggestions or []


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (ValueError, TypeError):
        return default


def _get_blocklist() -> frozenset:
    global _blocklist
    if _blocklist is not None:
        return _blocklist
    try:
        with open(_BLOCKLIST_PATH, encoding="utf-8") as f:
            _blocklist = frozenset(
                line.strip().lower() for line in f
                if line.strip() and not line.startswith("#")
            )
    except FileNotFoundError:
        logger.warning("Blocklist not found at %s", _BLOCKLIST_PATH)
        _blocklist = frozenset()
    return _blocklist


def validate_password_strength(
    password: str,
    *,
    blocklist: Optional[Iterable[str]] = None,
    min_length: Optional[int] = None,
    min_score: Optional[int] = None,
    user_inputs: Optional[List[str]] = None,
) -> None:
    """Raise PasswordPolicyError if password fails policy.

    All kwargs are test overrides; production uses env/defaults.
    user_inputs: terms (username, email) zxcvbn penalizes if embedded.
    """
    limit = min_length if min_length is not None else _env_int("AUTH_MIN_PASSWORD_LENGTH", _MIN_LENGTH)
    threshold = min_score if min_score is not None else _env_int("AUTH_MIN_ZXCVBN_SCORE", _MIN_SCORE)

    if len(password) < limit:
        raise PasswordPolicyError(
            f"Password must be at least {limit} characters long",
            suggestions=[f"Add {limit - len(password)} more characters."],
        )

    block = frozenset(b.lower() for b in blocklist) if blocklist is not None else _get_blocklist()
    if password.lower() in block:
        raise PasswordPolicyError(
            "Password is too common — choose something more unique",
            suggestions=["Avoid passwords from known breach lists."],
        )

    result = zxcvbn(password, user_inputs=user_inputs or [])
    if result["score"] >= threshold:
        return

    feedback = result.get("feedback", {})
    hints = list(feedback.get("suggestions", []))
    if w := feedback.get("warning"):
        hints.insert(0, w)
    if not hints:
        hints = ["Try a longer passphrase with unrelated words."]

    raise PasswordPolicyError(
        f"Password too weak (strength {result['score']}/4, need {threshold}/4)",
        suggestions=hints,
    )
