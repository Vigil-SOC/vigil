"""The key ring: one random key per purpose, each with a key id (#4180).

``auth_keys`` holds every key, wrapped (Fernet) under a key HKDF-derived from
``JWT_SECRET_KEY``, which stays outside the database. A purpose has one
``current`` key and, after a rotation, older ones: signing keys stay
``verify_only`` until their overlap ends (then ``retired``, material deleted),
the MFA key stays ``decrypt_only``.

Version 1 of each purpose is the value derived from the secret before the ring
existed, so tokens issued then keep verifying. Adoption writes version 1 and a
random version 2 at the first app start, under an advisory lock so one replica
does it. A purpose added later gets one random current key at the next start
(``_add_missing``). Only the API's startup adopts: a process holding the wrong
secret must not wrap a ring nobody else can read.

``python -m core.auth.key_ring rotate <purpose>`` rotates on the host.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import logging
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text

from core.config import get_settings
from core.secrets import get_secret, set_secret
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# Any replica notices a rotation made elsewhere within this long; an unknown
# kid reloads at once, at most every FORCED_REFRESH seconds.
REFRESH = 30.0
FORCED_REFRESH = 5.0
_LOCK_KEY = 0x7669_6769_6C4B_4559  # "vigilKEY"
_KEY_BITS = 256
_FERNET_TOKEN_PREFIX = "gAAAAA"  # MFA rows that are Fernet tokens, not legacy plaintext
SYSTEM = "system"


class KeyRingError(RuntimeError):
    """The ring cannot be read: not adopted yet, or wrapped under another secret."""


@dataclass(frozen=True)
class Purpose:
    overlap: Optional[timedelta]  # how long a signing key verifies after rotation
    derive: Callable[[str], bytes]  # version 1: what the secret gave before the ring

    @property
    def encrypts(self) -> bool:
        return self.overlap is None


def _hmac(tag: str) -> Callable[[str], bytes]:
    return lambda s: hmac.new(s.encode(), tag.encode(), hashlib.sha256).digest()


# Overlaps are each purpose's longest token: sign-ins 24 h or the refresh
# token's lifetime if longer, run tokens 31 d (tool_principal.RUN_TTL), reset
# links 1 h.
_SIGNIN_OVERLAP = max(
    timedelta(hours=24), timedelta(days=get_settings().jwt_refresh_expiration_days)
)
PURPOSES: dict[str, Purpose] = {
    "signin": Purpose(_SIGNIN_OVERLAP, lambda s: s.encode()),
    "run": Purpose(timedelta(days=31), _hmac("vigil-tool-principal")),
    "reset": Purpose(timedelta(hours=1), lambda s: s.encode()),
    "setup": Purpose(timedelta(hours=24), _hmac("vigil-bootstrap-setup")),
    "mfa": Purpose(None, lambda s: hashlib.sha256(s.encode()).digest()),
    # Security-key challenges (#4234): minutes long, bound to the account.
    "webauthn": Purpose(timedelta(minutes=10), _hmac("vigil-webauthn-challenge")),
}


@dataclass(frozen=True)
class Key:
    purpose: str
    version: int
    state: str
    secret: bytes
    until: Optional[datetime]  # end of a signing key's overlap

    @property
    def kid(self) -> str:
        return f"{self.purpose}-{self.version}"

    def live(self, now: datetime) -> bool:
        return self.until is None or self.until > now


# --- the secret that wraps the ring -----------------------------------------


def wrapping_secret() -> str:
    """Today's JWT_SECRET_KEY. Production fails closed when it is unset; dev
    generates one into the secret store, since no key ships in code. Never
    generated in production: Helm's state directory is per pod, so replicas
    would hold different ones."""
    value = get_secret("JWT_SECRET_KEY")
    if value:
        return value
    if not get_settings().dev_mode:
        raise RuntimeError(
            "JWT_SECRET_KEY is required when DEV_MODE=false. "
            "Set it in the environment or .env before starting the backend."
        )
    if not set_secret("JWT_SECRET_KEY", secrets.token_urlsafe(48)):
        raise RuntimeError("JWT_SECRET_KEY is unset and could not be generated")
    logger.warning("JWT_SECRET_KEY not set; generated one into the secret store")
    return get_secret("JWT_SECRET_KEY") or ""


def _wrapper(secret: str) -> Fernet:
    okm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"vigil-key-ring-wrap-v1"
    ).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(okm))


# --- reading ------------------------------------------------------------------

_lock = threading.RLock()
_cache: Optional[dict[str, list[Key]]] = None
_loaded = 0.0
_forced = float("-inf")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(session, secret: str) -> dict[str, list[Key]]:
    wrap = _wrapper(secret)
    rows = session.execute(text("""
            SELECT purpose, version, state, wrapped, verify_until
            FROM public.auth_keys WHERE state <> 'retired' ORDER BY purpose, version
        """)).all()
    ring: dict[str, list[Key]] = {}
    for purpose, version, state, wrapped, until in rows:
        try:
            material = wrap.decrypt(wrapped.encode())
        except InvalidToken as exc:
            raise KeyRingError(
                "the key ring is not wrapped under this JWT_SECRET_KEY"
            ) from exc
        ring.setdefault(purpose, []).append(
            Key(purpose, version, state, material, until)
        )
    return ring


def _snapshot(force: bool = False) -> dict[str, list[Key]]:
    global _cache, _loaded, _forced
    with _lock:
        now = time.monotonic()
        if force and now - _forced < FORCED_REFRESH:
            force = False
        elif force:
            _forced = now
        if _cache is None or force or now - _loaded > REFRESH:
            try:
                try:
                    retire_expired()
                except Exception as exc:  # reading must not depend on a write
                    logger.warning("key ring retirement failed: %s", exc)
                with unit_of_work() as session:
                    _cache = _read(session, wrapping_secret())
            except KeyRingError:
                raise
            except Exception as exc:
                if _cache is None:
                    raise KeyRingError(f"the key ring cannot be read: {exc}") from exc
                logger.warning("key ring refresh failed, using the last copy: %s", exc)
            _loaded = now
        if not _cache:
            raise KeyRingError("the key ring is empty: the API has not adopted yet")
        return _cache


def forget() -> None:
    """Drop the cached ring, e.g. after this process changed it."""
    global _cache
    with _lock:
        _cache = None


def current(purpose: str) -> Key:
    for key in _snapshot().get(purpose, []):
        if key.state == "current":
            return key
    raise KeyRingError(f"no current {purpose} key")


def live(purpose: str, *, force: bool = False) -> list[Key]:
    """Every key that may still verify or decrypt, oldest first."""
    now = _now()
    return [k for k in _snapshot(force).get(purpose, []) if k.live(now)]


def find(purpose: str, version: int) -> Optional[Key]:
    for force in (False, True):  # an unknown version may be a rotation elsewhere
        for key in live(purpose, force=force):
            if key.version == version:
                return key
    return None


def token_key(purpose: str, token: str) -> bytes:
    """The secret a JWT's ``kid`` names. A token without one verifies only under
    version 1, and only while that is still live."""
    kid = jwt.get_unverified_header(token).get("kid")
    version = 1
    if kid is not None:
        prefix, _, number = str(kid).rpartition("-")
        if prefix != purpose or not (number.isascii() and number.isdigit()):
            raise jwt.InvalidTokenError("unknown key id")
        version = int(number)
    key = find(purpose, version)
    if key is None:
        raise jwt.InvalidTokenError("unknown key id")
    return key.secret


def sign(purpose: str, payload: dict, algorithm: str = "HS256") -> str:
    key = current(purpose)
    return jwt.encode(
        payload, key.secret, algorithm=algorithm, headers={"kid": key.kid}
    )


def _fernet(material: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(material))


def encryptor_from(keys: list[Key]) -> MultiFernet:
    """Encrypts under the current key, decrypts under any of ``keys``."""
    ordered = sorted(keys, key=lambda k: k.state != "current")
    return MultiFernet([_fernet(k.secret) for k in ordered])


def encryptor(purpose: str = "mfa", *, force: bool = False) -> MultiFernet:
    return encryptor_from(live(purpose, force=force))


# --- writing ------------------------------------------------------------------


def _wrap(secret: str, material: bytes) -> str:
    return _wrapper(secret).encrypt(material).decode()


def _trail(session, action: str, operator: Optional[str] = None, **fields) -> None:
    """A system entry in the caller's transaction. Not gated: a restore's pause
    must not stop the keys it rotates from being recorded."""
    from core.audit import trail
    from core.auth import trail_events

    if trail_events.ready(session):
        beside = {"operator_text": operator} if operator else {}
        trail.append(
            session,
            trail_events.payload(action, actor=SYSTEM, **fields),
            gated=False,
            **beside,
        )


def _insert(
    session, secret: str, purpose, version, state, material, until=None
) -> None:
    session.execute(
        text("""
            INSERT INTO public.auth_keys (purpose, version, state, wrapped, verify_until)
            VALUES (:p, :v, :s, :w, :u)
        """),
        {
            "p": purpose,
            "v": version,
            "s": state,
            "w": _wrap(secret, material),
            "u": until,
        },
    )


def _new_material() -> bytes:
    return secrets.token_bytes(_KEY_BITS // 8)


def _retire(session, purpose, version, *, reason: str, operator=None) -> None:
    session.execute(
        text("""
            UPDATE public.auth_keys SET state = 'retired', wrapped = NULL,
                retired_at = now(), verify_until = NULL
            WHERE purpose = :p AND version = :v
        """),
        {"p": purpose, "v": version},
    )
    _trail(
        session,
        "auth.key_retired",
        operator,
        purpose=purpose,
        version=version,
        reason=reason,
    )


def retire_expired() -> None:
    """Delete the material of signing keys whose overlap has ended."""
    with unit_of_work() as session:
        rows = session.execute(text("""
                UPDATE public.auth_keys SET state = 'retired', wrapped = NULL,
                    retired_at = now(), verify_until = NULL
                WHERE state = 'verify_only' AND verify_until <= now()
                RETURNING purpose, version
            """)).all()
        for purpose, version in rows:
            _trail(
                session,
                "auth.key_retired",
                purpose=purpose,
                version=version,
                reason="overlap_ended",
            )
    if rows:
        logger.info("key ring: retired %s", ", ".join(f"{p}-{v}" for p, v in rows))


def _advisory(session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})


def _reencrypt_mfa(session, fernet: MultiFernet) -> int:
    """Move every MFA secret that is a Fernet token under ``fernet``'s first key.
    Legacy plaintext and tokens no key can read are left as they are."""
    rows = session.execute(
        text("""
            SELECT user_id, mfa_secret FROM public.users
            WHERE mfa_secret LIKE :prefix FOR UPDATE
        """),
        {"prefix": _FERNET_TOKEN_PREFIX + "%"},
    ).all()
    moved = 0
    for user_id, token in rows:
        try:
            rotated = fernet.rotate(token.encode()).decode()
        except InvalidToken:
            logger.error(
                "MFA secret of user %s is not under a key in the ring", user_id
            )
            continue
        session.execute(
            text("UPDATE public.users SET mfa_secret = :s WHERE user_id = :u"),
            {"s": rotated, "u": user_id},
        )
        moved += 1
    return moved


def _add_missing(session, secret: str) -> list[str]:
    """A current key for each purpose the adopted ring lacks, as a fresh random
    version 1: nothing derived from the secret existed for it before."""
    held = set(
        session.execute(text("SELECT DISTINCT purpose FROM public.auth_keys"))
        .scalars()
        .all()
    )
    missing = sorted(set(PURPOSES) - held)
    if missing:
        _read(session, secret)  # a wrong secret must not add a row nobody can open
        for purpose in missing:
            _insert(session, secret, purpose, 1, "current", _new_material())
        _trail(session, "auth.keys_adopted", purposes=missing, mfa_secrets=0)
    return missing


def adopt(session=None, *, secret: Optional[str] = None) -> bool:
    """Write version 1 (the derived value) and version 2 (random) of each
    purpose, move MFA secrets under the new key. On a ring already adopted, add
    a key for any purpose it lacks. False when there was nothing to write."""
    secret = secret or wrapping_secret()
    with unit_of_work(session) as session:
        _advisory(session)  # the winner commits; the others then see its rows
        if session.execute(text("SELECT 1 FROM public.auth_keys LIMIT 1")).first():
            added = _add_missing(session, secret)
            forget()  # another replica adopted, or this added keys
            if added:
                logger.info("key ring: added %s", ", ".join(added))
            return bool(added)
        now = _now()
        fresh = {}
        for purpose, spec in PURPOSES.items():
            v1 = spec.derive(secret)
            if spec.encrypts:
                _insert(session, secret, purpose, 1, "decrypt_only", v1)
            else:
                _insert(
                    session, secret, purpose, 1, "verify_only", v1, now + spec.overlap
                )
            fresh[purpose] = _new_material()
            _insert(session, secret, purpose, 2, "current", fresh[purpose])
        moved = _reencrypt_mfa(
            session,
            MultiFernet(
                [_fernet(fresh["mfa"]), _fernet(PURPOSES["mfa"].derive(secret))]
            ),
        )
        _trail(
            session, "auth.keys_adopted", purposes=sorted(PURPOSES), mfa_secrets=moved
        )
    forget()
    logger.info(
        "key ring adopted: %d purposes, %d MFA secrets moved", len(PURPOSES), moved
    )
    return True


def rotate(
    purpose: str,
    *,
    session=None,
    secret: Optional[str] = None,
    overlap: Optional[timedelta] = None,
    operator: Optional[str] = None,
) -> int:
    """A new current key for ``purpose``; the old one verifies for ``overlap``
    (the purpose's own by default, none for a restore). The MFA key goes
    decrypt-only and every secret moves under the new one. Returns the version."""
    spec = PURPOSES[purpose]
    secret = secret or wrapping_secret()
    overlap = spec.overlap if overlap is None else overlap
    with unit_of_work(session) as session:
        _advisory(session)
        row = session.execute(
            text("""
                SELECT version FROM public.auth_keys
                WHERE purpose = :p AND state = 'current' FOR UPDATE
            """),
            {"p": purpose},
        ).first()
        if row is None:
            raise KeyRingError(f"no current {purpose} key to rotate")
        _read(session, secret)  # a wrong secret must not add a row nobody can open
        old, version = row[0], row[0] + 1
        new_material = _new_material()
        if spec.encrypts:
            session.execute(
                text(
                    "UPDATE public.auth_keys SET state = 'decrypt_only' "
                    "WHERE purpose = :p AND version = :v"
                ),
                {"p": purpose, "v": old},
            )
        elif overlap > timedelta(0):
            session.execute(
                text(
                    "UPDATE public.auth_keys SET state = 'verify_only', "
                    "verify_until = :u WHERE purpose = :p AND version = :v"
                ),
                {"p": purpose, "v": old, "u": _now() + overlap},
            )
        else:  # no overlap: nothing but the new key verifies, earlier overlaps included
            held = (
                session.execute(
                    text(
                        "SELECT version FROM public.auth_keys WHERE purpose = :p "
                        "AND state IN ('current', 'verify_only') ORDER BY version"
                    ),
                    {"p": purpose},
                )
                .scalars()
                .all()
            )
            for v in held:
                _retire(
                    session,
                    purpose,
                    v,
                    reason="rotated_without_overlap",
                    operator=operator,
                )
        _insert(session, secret, purpose, version, "current", new_material)
        moved = None
        if spec.encrypts:  # the key just written reads as current
            moved = _reencrypt_mfa(
                session, encryptor_from(_read(session, secret)[purpose])
            )
        _trail(
            session,
            "auth.key_rotated",
            operator,
            purpose=purpose,
            version=version,
            previous=old,
            mfa_secrets=moved,
            overlap_seconds=None if spec.encrypts else int(overlap.total_seconds()),
        )
    forget()
    return version


def status() -> list[tuple]:
    with unit_of_work() as session:
        return session.execute(text("""
                SELECT purpose, version, state, created_at, verify_until
                FROM public.auth_keys WHERE state <> 'retired' ORDER BY purpose, version
            """)).all()


# --- start-up ------------------------------------------------------------------


async def adopt_when_ready() -> None:
    """Run as a background task: on Helm the schema arrives after the pods
    start, so a failed attempt retries (capped backoff) until it works."""
    import asyncio

    delay = 2.0
    while True:
        try:
            await asyncio.to_thread(adopt)
            return
        except Exception as exc:  # noqa: BLE001 - the table may not exist yet
            logger.warning("key ring adoption waiting: %s", exc)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


# --- host command --------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.auth.key_ring")
    sub = parser.add_subparsers(dest="command", required=True)
    rot = sub.add_parser(
        "rotate", help="rotate one purpose, keeping the old key's overlap"
    )
    rot.add_argument("purpose", choices=sorted(PURPOSES))
    sub.add_parser("status", help="list the keys that are not retired")
    args = parser.parse_args(argv)

    from core.storage.connection import init_database

    init_database(echo=False)
    if args.command == "status":
        for purpose, version, state, created, until in status():
            print(
                f"{purpose}-{version}\t{state}\t{created:%Y-%m-%d %H:%M}\t"
                f"{'until ' + format(until, '%Y-%m-%d %H:%M') if until else ''}"
            )
        return 0
    try:
        version = rotate(args.purpose, operator=getpass.getuser())
    except KeyRingError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"{args.purpose} is now at version {version}; replicas pick it up within "
        f"{int(REFRESH)} seconds"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
