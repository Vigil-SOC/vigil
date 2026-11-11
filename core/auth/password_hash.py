"""Password and recovery-code hashing: one scheme, in one place (#4174).

New hashes are PBKDF2-HMAC-SHA-256 (``cryptography``), self-describing:
``pbkdf2-sha256$<iterations>$<salt b64>$<hash b64>``. bcrypt (``$2``) hashes
from earlier installs verify only; ``needs_rewrite`` tells the caller to
replace one after a good sign-in. Input is NFKC-normalised, never truncated.

A PBKDF2 hash is stored encrypted under the key ring's ``pepper`` key (#4298):
``pepper-<version>$<Fernet token>``, the token wrapping the string above. A
database copy without the ring can check no password. bcrypt entries stay bare.
A ring that cannot be read raises ``KeyRingError`` (never a "no match"); a
wrapped entry no key opens simply never matches.
"""

import base64
import hmac
import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import bcrypt
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core.auth import key_ring

SCHEME = "pbkdf2-sha256"
# OWASP Password Storage Cheat Sheet: 600,000 for PBKDF2-HMAC-SHA256.
ITERATIONS = 600_000
# Any Unicode, counted in code points (len(str)), never cut short.
MAX_PASSWORD_LENGTH = 128
_SALT_BYTES = 16
_KEY_BYTES = 32
PEPPER = "pepper"
_PEPPER_PREFIX = PEPPER + "-"


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


def seal(plain: str, key: key_ring.Key | None = None) -> str:
    """Encrypt a PBKDF2 string under ``key`` (the current pepper by default)."""
    key = key or key_ring.current(PEPPER)
    token = key_ring.encryptor_from([key]).encrypt(plain.encode()).decode()
    return f"{key.kid}${token}"


def is_plain_pbkdf2(entry: str) -> bool:
    return isinstance(entry, str) and entry.startswith(SCHEME + "$")


def _open(entry: str) -> str:
    """The bare hash behind a stored entry; "" when it is wrapped and no key
    opens it (so it never matches)."""
    if not entry or not entry.startswith(_PEPPER_PREFIX):
        return entry
    kid, _, token = entry.partition("$")
    try:
        key = key_ring.find(PEPPER, int(kid[len(_PEPPER_PREFIX) :]))
        if key is None:
            return ""
        return key_ring.encryptor_from([key]).decrypt(token.encode()).decode()
    except (ValueError, InvalidToken):
        return ""


def hash(secret: str) -> str:
    return seal(_format(secret, os.urandom(_SALT_BYTES)))


def hash_set(secrets: list[str]) -> list[str]:
    """Hash a set of random codes under one salt, so a submitted code is derived
    once (``find``) however many are stored. Fine for 64-bit random values;
    never for anything a person chose."""
    salt = os.urandom(_SALT_BYTES)
    return [seal(_format(s, salt)) for s in secrets]


def _is_bcrypt(stored: str) -> bool:
    return stored.startswith("$2")


def _bcrypt_ok(candidate: bytes, entry: str) -> bool:
    try:
        return bcrypt.checkpw(candidate, entry.encode("utf-8"))
    except ValueError:
        return False  # over bcrypt's 72 bytes (NFKC can grow a string): not a match


def _derive_all(normal: bytes, stored: list[str]) -> dict[tuple[str, int], bytes]:
    """Derive for every distinct salt and count in ``stored`` at once: each
    derivation is slow (up to 25 of them for a full password history) and
    releases the GIL, so they run side by side."""
    wanted = set()
    for entry in stored:
        parts = (entry or "").split("$")
        try:
            if len(parts) == 4 and parts[0] == SCHEME:
                wanted.add((parts[2], int(parts[1])))
        except ValueError:
            continue
    if len(wanted) < 2:
        return {}
    todo = list(wanted)

    def one(key):
        try:
            return _derive(normal, base64.b64decode(key[0]), key[1])
        except ValueError:
            return None  # malformed: the loop below skips the entry

    with ThreadPoolExecutor(max_workers=min(len(todo), os.cpu_count() or 1)) as pool:
        return {k: v for k, v in zip(todo, pool.map(one, todo)) if v is not None}


def find(secret: str, stored: list[str]) -> int | None:
    """Index of the first hash in ``stored`` that matches ``secret``, else None.
    PBKDF2 hashes sharing a salt and count cost one derivation between them.
    A malformed hash never matches."""
    if len(secret) > MAX_PASSWORD_LENGTH:
        return None
    normal = _normalise(secret)
    # Open every wrapped entry first, then derive once per (salt, count).
    stored = [_open(entry) for entry in stored]
    derived = _derive_all(normal, stored)
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
    stored = _open(stored)
    if not stored:
        return False
    if _is_bcrypt(stored):
        return True
    parts = stored.split("$")
    try:
        return parts[0] != SCHEME or int(parts[1]) < ITERATIONS
    except (IndexError, ValueError):
        return False
