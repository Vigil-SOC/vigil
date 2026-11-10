"""Password and recovery-code hashing: one scheme, in one place (#4174).

New hashes are PBKDF2-HMAC-SHA-256 (``cryptography``), self-describing:
``pbkdf2-sha256$<iterations>$<salt b64>$<hash b64>``. bcrypt (``$2``) hashes
from earlier installs verify only; ``needs_rewrite`` tells the caller to
replace one after a good sign-in. Input is NFKC-normalised, never truncated.
"""

import base64
import hmac
import os
import unicodedata

import bcrypt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SCHEME = "pbkdf2-sha256"
# OWASP Password Storage Cheat Sheet: 600,000 for PBKDF2-HMAC-SHA256.
ITERATIONS = 600_000
# Any Unicode, counted in code points (len(str)), never cut short.
MAX_PASSWORD_LENGTH = 128
_SALT_BYTES = 16
_KEY_BYTES = 32


def _normalise(secret: str) -> bytes:
    return unicodedata.normalize("NFKC", secret).encode("utf-8")


def _derive(secret: bytes, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(secret)


def _format(secret: str, salt: bytes) -> str:
    key = _derive(_normalise(secret), salt, ITERATIONS)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"{SCHEME}${ITERATIONS}${b64(salt)}${b64(key)}"


def hash(secret: str) -> str:
    return _format(secret, os.urandom(_SALT_BYTES))


def hash_set(secrets: list[str]) -> list[str]:
    """Hash a set of random codes under one salt, so a submitted code is derived
    once (``find``) however many are stored. Fine for 64-bit random values;
    never for anything a person chose."""
    salt = os.urandom(_SALT_BYTES)
    return [_format(s, salt) for s in secrets]


def _is_bcrypt(stored: str) -> bool:
    return stored.startswith("$2")


def _bcrypt_ok(candidate: bytes, entry: str) -> bool:
    try:
        return bcrypt.checkpw(candidate, entry.encode("utf-8"))
    except ValueError:
        return False  # over bcrypt's 72 bytes (NFKC can grow a string): not a match


def find(secret: str, stored: list[str]) -> int | None:
    """Index of the first hash in ``stored`` that matches ``secret``, else None.
    PBKDF2 hashes sharing a salt and count cost one derivation between them.
    A malformed hash never matches."""
    if len(secret) > MAX_PASSWORD_LENGTH:
        return None
    normal = _normalise(secret)
    derived: dict[tuple[str, int], bytes] = {}
    for i, entry in enumerate(stored):
        try:
            if not entry:
                continue
            if _is_bcrypt(entry):
                # A bcrypt hash was made from the raw string before NFKC; accept
                # either form so no existing password stops working.
                raw = secret.encode("utf-8")
                if _bcrypt_ok(normal, entry) or (
                    raw != normal and _bcrypt_ok(raw, entry)
                ):
                    return i
                continue
            scheme, iterations, salt, key = entry.split("$")
            if scheme != SCHEME:
                continue
            if (salt, int(iterations)) not in derived:
                derived[(salt, int(iterations))] = _derive(
                    normal, base64.b64decode(salt), int(iterations)
                )
            if hmac.compare_digest(
                derived[(salt, int(iterations))], base64.b64decode(key)
            ):
                return i
        except (ValueError, TypeError, AttributeError):
            continue  # malformed entry
    return None


def verify(secret: str, stored: str) -> bool:
    return find(secret, [stored]) is not None


def needs_rewrite(stored: str) -> bool:
    """True for a bcrypt hash or a PBKDF2 one below the current iteration count."""
    if not stored:
        return False
    if _is_bcrypt(stored):
        return True
    parts = stored.split("$")
    try:
        return parts[0] != SCHEME or int(parts[1]) < ITERATIONS
    except (IndexError, ValueError):
        return False
