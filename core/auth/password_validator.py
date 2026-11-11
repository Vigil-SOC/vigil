"""Password strength validation using zxcvbn entropy scoring (NIST 800-63B)."""

import logging
import math
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from zxcvbn import zxcvbn

from core.auth import breached_list, session_policy
from core.auth.password_hash import MAX_PASSWORD_LENGTH
from core.platform import notices
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# zxcvbn score a password must reach (0-4). Fixed: AUTH_MIN_ZXCVBN_SCORE is retired.
MIN_ZXCVBN_SCORE = 3

_BLOCKLIST_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "common_passwords.txt"
)
_blocklist: Optional[frozenset] = None


class PasswordPolicyError(ValueError):
    def __init__(self, message: str, *, suggestions: Optional[List[str]] = None):
        super().__init__(message)
        self.suggestions = suggestions or []

    def as_detail(self) -> str:
        """Render message + suggestions as a single client-facing string.

        Callers surface this via HTTPException(detail=...); the frontend
        renders ``detail`` as a plain string, so suggestions are folded
        in here rather than returned as a nested object.
        """
        if not self.suggestions:
            return str(self)
        return f"{self} ({'; '.join(self.suggestions)})"


def _get_blocklist() -> frozenset:
    global _blocklist
    if _blocklist is not None:
        return _blocklist
    try:
        with open(_BLOCKLIST_PATH, encoding="utf-8") as f:
            # One entry per line, as the source has it; an entry may start with "#".
            _blocklist = frozenset(
                line.rstrip("\r\n").lower() for line in f if line.strip()
            )
    except FileNotFoundError:
        logger.warning("Blocklist not found at %s", _BLOCKLIST_PATH)
        _blocklist = frozenset()
    return _blocklist


def check_not_listed(
    password: str,
    *,
    user_inputs: Optional[Iterable[str]] = None,
    blocklist: Optional[Iterable[str]] = None,
) -> None:
    """Raise PasswordPolicyError if the whole password is a breached-list entry,
    "Vigil", or one of ``user_inputs`` (username, email). Case-insensitive.

    Cheap enough to run at every sign-in, unlike the zxcvbn pass."""
    folded = password.lower()
    if folded == "vigil":
        raise PasswordPolicyError(
            'Password can\'t be "Vigil"',
            suggestions=["Choose something unrelated to this product."],
        )
    if any(t and folded == t.lower() for t in user_inputs or ()):
        raise PasswordPolicyError(
            "Password can't be your username or email address",
            suggestions=["Choose something unrelated to this account."],
        )
    block = (
        frozenset(b.lower() for b in blocklist)
        if blocklist is not None
        else _get_blocklist()
    )
    # The shipped list, then entries an administrator imported. An explicit
    # ``blocklist`` stands alone.
    if folded in block or (blocklist is None and breached_list.is_imported(folded)):
        raise PasswordPolicyError(
            "Password appears in a list of breached passwords",
            suggestions=["Choose something more unique."],
        )


def _classes(password: str) -> set:
    """Character classes present, by Unicode category: letters (any script),
    lowercase, uppercase, digits, and anything else. Nothing is normalised away."""
    found = set()
    for ch in password:
        cat = unicodedata.category(ch)
        if cat == "Nd":
            found.add("digit")
        elif cat.startswith("L"):
            found.add("letter")
            if cat == "Ll":
                found.add("lowercase")
            elif cat == "Lu":
                found.add("uppercase")
        else:
            found.add("symbol")
    return found


_RULE_NEEDS = {
    "letters_digits": (("letter", "a letter"), ("digit", "a digit")),
    "four_classes": (
        ("lowercase", "a lowercase letter"),
        ("uppercase", "an uppercase letter"),
        ("digit", "a digit"),
        ("symbol", "a symbol or space"),
    ),
}


def check_character_rules(password: str, rule: str) -> None:
    """Raise PasswordPolicyError naming each class the policy's rule still needs."""
    missing = [
        name for key, name in _RULE_NEEDS.get(rule, ()) if key not in _classes(password)
    ]
    if missing:
        raise PasswordPolicyError(
            "Password must contain " + ", ".join(missing),
            suggestions=[f"Add {', '.join(missing)}."],
        )


def validate_password_strength(
    password: str,
    *,
    blocklist: Optional[Iterable[str]] = None,
    min_length: Optional[int] = None,
    min_score: Optional[int] = None,
    user_inputs: Optional[List[str]] = None,
) -> None:
    """Raise PasswordPolicyError if password fails policy.

    The minimum length and character rule come from the session policy; the
    kwargs override for tests.
    user_inputs: terms (username, email) zxcvbn penalizes if embedded.
    """
    policy = session_policy.policy()
    limit = min_length if min_length is not None else policy.password_min_length
    threshold = min_score if min_score is not None else MIN_ZXCVBN_SCORE
    # zxcvbn scores run 0-4; clamp so a caller's 5+ can't reject every
    # password and a negative value can't underflow the check.
    threshold = max(0, min(4, threshold))

    if len(password) < limit:
        raise PasswordPolicyError(
            f"Password must be at least {limit} characters long",
            suggestions=[f"Add {limit - len(password)} more characters."],
        )

    if len(password) > MAX_PASSWORD_LENGTH:  # before the costly zxcvbn pass
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters long",
            suggestions=["Use a shorter passphrase."],
        )

    check_character_rules(password, policy.password_character_rules)
    check_not_listed(password, user_inputs=user_inputs, blocklist=blocklist)

    result = zxcvbn(
        password, user_inputs=user_inputs or [], max_length=MAX_PASSWORD_LENGTH
    )
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


# --- Password age (max / min) ---

WARN_DAYS = 7
CHANGE_LINK = "/overview?change_password=1"
USERS_LINK = "/settings?section=users"


def _age_start(user: User) -> datetime:
    return user.password_changed_at or user.created_at


def expires_at(user: User, max_age_days: Optional[int]) -> Optional[datetime]:
    """When ``user``'s password passes the maximum age; None when it has none."""
    if max_age_days is None:
        return None
    return _age_start(user) + timedelta(days=max_age_days)


def changeable_at(user: User, min_age_days: int) -> Optional[datetime]:
    """When a voluntary change is next allowed; None when it is now."""
    if not min_age_days or user.password_changed_at is None:
        return None
    at = user.password_changed_at + timedelta(days=min_age_days)
    return at if at > utcnow() else None


def check_not_expired(user: User) -> None:
    """Raise PasswordPolicyError when ``user``'s password is past the maximum age."""
    due = expires_at(user, session_policy.policy().password_max_age_days)
    if due is not None and due <= utcnow():
        raise PasswordPolicyError(
            f"Password is older than the maximum age and expired on {due:%Y-%m-%d}"
        )


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def expiry_warning(user: User) -> list[dict[str, str]]:
    """The bell's condition for a person whose password expires within a week."""
    due = expires_at(user, session_policy.policy().password_max_age_days)
    # No password (SSO-only person; stored as "") means nothing to change
    if due is None or user.service_account is True or not user.password_hash:
        return []
    left = math.ceil((due - utcnow()).total_seconds() / 86400)
    if left > WARN_DAYS:
        return []
    message = (
        f"Your password expires in {_days(left)}"
        if left > 0
        else "Your password has expired"
    )
    return [{"message": message, "link": CHANGE_LINK}]


def stale_service_accounts(user: User) -> list[dict[str, str]]:
    """For a holder of ``users.write``: service accounts past the maximum age."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "users.write") is not None:
        return []
    max_age = session_policy.policy().password_max_age_days
    if max_age is None:
        return []
    with unit_of_work() as session:
        accounts = (
            session.query(User)
            .filter(User.is_active.is_(True), User.service_account.is_(True))
            .all()
        )
        # Token-only accounts have no password to rotate
        names = sorted(
            a.username
            for a in accounts
            if a.password_hash and expires_at(a, max_age) <= utcnow()
        )
    if not names:
        return []
    many = len(names) > 1
    noun = (
        "service accounts' passwords are" if many else "service account's password is"
    )
    message = (
        f"{len(names)} {noun} past the maximum age: {', '.join(names)}. "
        f"Rotate {'them' if many else 'it'}."
    )
    return [{"message": message, "link": USERS_LINK}]


def register() -> None:
    notices.CONDITIONS.extend([expiry_warning, stale_service_accounts])
    breached_list.register()
