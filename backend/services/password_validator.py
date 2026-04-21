"""
Password strength validation.

Policy (length-forward, NIST 800-63B aligned):
- Minimum length (AUTH_MIN_PASSWORD_LENGTH, default 12)
- At least one letter
- At least one digit
- Not in the bundled common-password blocklist

No forced symbols or mixed-case. NIST recommends length over complexity.
The blocklist kills the cases where complexity rules typically bite
("Password1!" trivially passes complexity but appears in breach corpora).
"""

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


DEFAULT_MIN_LENGTH = 12
DEFAULT_BLOCKLIST_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "common_passwords.txt"


class PasswordPolicyError(ValueError):
    """Raised when a password fails the validation policy."""


def _load_blocklist(path: Optional[Path] = None) -> frozenset:
    """Load the common-password blocklist. Cached in the module state."""
    path = path or DEFAULT_BLOCKLIST_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = {
                line.strip().lower()
                for line in f
                if line.strip() and not line.startswith("#")
            }
        return frozenset(entries)
    except FileNotFoundError:
        logger.warning(
            "Common-password blocklist not found at %s — blocklist check disabled",
            path,
        )
        return frozenset()


_blocklist: Optional[frozenset] = None


def _blocklist_cached() -> frozenset:
    global _blocklist
    if _blocklist is None:
        _blocklist = _load_blocklist()
    return _blocklist


def _min_length() -> int:
    try:
        return int(os.getenv("AUTH_MIN_PASSWORD_LENGTH", str(DEFAULT_MIN_LENGTH)))
    except ValueError:
        return DEFAULT_MIN_LENGTH


def validate_password_strength(
    password: str,
    *,
    blocklist: Optional[Iterable[str]] = None,
    min_length: Optional[int] = None,
) -> None:
    """
    Raise PasswordPolicyError if the password does not meet policy.

    Arguments are overridable for testing; production code calls this with
    no arguments and picks up env / bundled-blocklist defaults.
    """
    limit = min_length if min_length is not None else _min_length()
    if len(password) < limit:
        raise PasswordPolicyError(
            f"Password must be at least {limit} characters long"
        )

    if not any(c.isalpha() for c in password):
        raise PasswordPolicyError("Password must contain at least one letter")

    if not any(c.isdigit() for c in password):
        raise PasswordPolicyError("Password must contain at least one digit")

    block = frozenset(b.lower() for b in blocklist) if blocklist is not None else _blocklist_cached()
    if password.lower() in block:
        raise PasswordPolicyError(
            "Password is too common — choose something more unique"
        )
