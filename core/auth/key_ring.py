"""The key ring: one random key per purpose, each with a key id (#4180).

``auth_keys`` holds every key, wrapped (Fernet) under a key HKDF-derived from
``JWT_SECRET_KEY``, which stays outside the database. A purpose has one
``current`` key and, after a rotation, older ones: signing keys stay
``verify_only`` until their overlap ends (then ``retired``, material deleted),
the MFA and pepper keys stay ``decrypt_only``.

Version 1 of each purpose is the value derived from the secret before the ring
existed, so tokens issued then keep verifying. Adoption writes version 1 and a
random version 2 at the first app start, under an advisory lock so one replica
does it. A purpose added later gets one random current key at the next start
(``_add_missing``). Only the API's startup adopts: a process holding the wrong
secret must not wrap a ring nobody else can read.

``python -m core.auth.key_ring`` runs on the host: ``rotate <purpose>``,
``emergency <purpose|all>`` (no overlap, stops every run), ``reset-mfa``,
``rewrap`` (after the operator changed JWT_SECRET_KEY), ``status`` and
``inventory`` (every key, retired too, never the material).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
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
    # Version 1: what the secret gave before the ring. None: nothing did, so the
    # purpose starts at one random key, as a purpose added later does.
    derive: Optional[Callable[[str], bytes]]

    @property
    def encrypts(self) -> bool:
        return self.overlap is None


def _hmac(tag: str) -> Callable[[str], bytes]:
    return lambda s: hmac.new(s.encode(), tag.encode(), hashlib.sha256).digest()


# A refresh token's lifetime. Fixed: JWT_REFRESH_EXPIRATION_DAYS is retired.
REFRESH_EXPIRATION_DAYS = 7

# Overlaps are each purpose's longest token: sign-ins 24 h or the refresh
# token's lifetime if longer, run tokens 31 d (tool_principal.RUN_TTL), single
# sign-on state 5 min. Reset links are rows of auth_links now (#4227): their
# purpose is gone, and a stored key for it is retired at the next start.
_SIGNIN_OVERLAP = max(timedelta(hours=24), timedelta(days=REFRESH_EXPIRATION_DAYS))
PURPOSES: dict[str, Purpose] = {
    "signin": Purpose(_SIGNIN_OVERLAP, lambda s: s.encode()),
    "run": Purpose(timedelta(days=31), _hmac("vigil-tool-principal")),
    "setup": Purpose(timedelta(hours=24), _hmac("vigil-bootstrap-setup")),
    "mfa": Purpose(None, lambda s: hashlib.sha256(s.encode()).digest()),
    # The pepper (#4298): password and recovery-code hashes are encrypted under it.
    "pepper": Purpose(None, None),
    # Security-key challenges (#4234): minutes long, bound to the account.
    "webauthn": Purpose(timedelta(minutes=10), _hmac("vigil-webauthn-challenge")),
    # Single sign-on's browser binding (#4284): state, nonce and verifier, 5 minutes.
    "sso": Purpose(timedelta(minutes=5), _hmac("vigil-sso-state")),
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


def _rewrite_hashes(
    session, marker: str, change: Callable[[str], Optional[str]]
) -> int:
    """Replace each stored hash ``change`` returns a new string for, in
    ``users.password_hash``, ``password_history`` and ``mfa_recovery_codes``, on
    rows that carry ``marker``. Returns how many entries changed."""
    rows = session.execute(
        text("""
            SELECT user_id, password_hash, password_history, mfa_recovery_codes
            FROM public.users WHERE password_hash LIKE :m
                OR password_history::text LIKE :j OR mfa_recovery_codes::text LIKE :j
            FOR UPDATE
        """),
        {"m": marker + "%", "j": '%"' + marker + "%"},
    ).all()

    def swap(entries):
        out = [change(e) if isinstance(e, str) else None for e in entries or []]
        return [n or e for n, e in zip(out, entries or [])], sum(
            n is not None for n in out
        )

    moved = 0
    for user_id, current, history, codes in rows:
        (new_current,), a = swap([current])
        new_history, b = swap(history)
        new_codes, c = swap(codes)
        if a + b + c:
            session.execute(
                text("""
                    UPDATE public.users SET password_hash = :p,
                        password_history = CAST(:h AS jsonb),
                        mfa_recovery_codes = CAST(:c AS jsonb) WHERE user_id = :u
                """),
                {
                    "p": new_current,
                    "h": json.dumps(new_history),
                    "c": json.dumps(new_codes),
                    "u": user_id,
                },
            )
        moved += a + b + c
    return moved


def _reencrypt_hashes(session, keys: list[Key]) -> int:
    """Move every pepper-wrapped hash under the current key, naming it. Tokens no
    key opens are left as they are."""
    fernet = encryptor_from(keys)
    kid = next(k.kid for k in keys if k.state == "current")

    def move(entry: str) -> Optional[str]:
        name, _, token = entry.partition("$")
        if not name.startswith("pepper-"):
            return None
        try:
            return f"{kid}${fernet.rotate(token.encode()).decode()}"
        except InvalidToken:
            logger.error("A stored hash is not under a key in the ring (%s)", name)
            return None

    return _rewrite_hashes(session, "pepper-", move)


def _wrap_hashes(session, secret: str) -> int:
    """Wrap every bare PBKDF2 hash under the current pepper (bcrypt and empty
    entries stay as they are); a second run finds nothing. Does nothing when
    ``secret`` cannot open the ring: only a process that can read it may wrap."""
    from core.auth import password_hash

    try:
        keys = _read(session, secret).get("pepper", [])
    except KeyRingError:
        return 0
    current = next((k for k in keys if k.state == "current"), None)
    if current is None:
        return 0
    return _rewrite_hashes(
        session,
        password_hash.SCHEME + "$",
        lambda e: (
            password_hash.seal(e, current) if password_hash.is_plain_pbkdf2(e) else None
        ),
    )


def _add_missing(session, secret: str) -> list[str]:
    """A current key for each purpose the adopted ring lacks, as a fresh random
    version 1: nothing derived from the secret existed for it before."""
    held = set(
        session.execute(text("SELECT DISTINCT purpose FROM public.auth_keys"))
        .scalars()
        .all()
    )
    # A purpose no longer used (reset, #4227): its keys' material is deleted.
    for purpose, version in session.execute(
        text(
            "SELECT purpose, version FROM public.auth_keys "
            "WHERE state <> 'retired' AND NOT (purpose = ANY(:known))"
        ),
        {"known": sorted(PURPOSES)},
    ).all():
        _retire(session, purpose, version, reason="purpose_retired")
    missing = sorted(set(PURPOSES) - held)
    if missing:
        _read(session, secret)  # a wrong secret must not add a row nobody can open
        for purpose in missing:
            _insert(session, secret, purpose, 1, "current", _new_material())
        _trail(
            session,
            "auth.keys_adopted",
            purposes=missing,
            mfa_secrets=0,
            password_hashes=_wrap_hashes(session, secret) if "pepper" in missing else 0,
        )
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
            if "pepper" not in added and (wrapped := _wrap_hashes(session, secret)):
                # A hash an older replica wrote since: wrap it too.
                _trail(
                    session,
                    "auth.keys_adopted",
                    purposes=[],
                    mfa_secrets=0,
                    password_hashes=wrapped,
                )
            forget()  # another replica adopted, or this added keys
            if added:
                logger.info("key ring: added %s", ", ".join(added))
            return bool(added)
        now = _now()
        fresh = {}
        for purpose, spec in PURPOSES.items():
            if spec.derive is None:
                fresh[purpose] = _new_material()
                _insert(session, secret, purpose, 1, "current", fresh[purpose])
                continue
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
        wrapped = _wrap_hashes(session, secret)
        _trail(
            session,
            "auth.keys_adopted",
            purposes=sorted(PURPOSES),
            mfa_secrets=moved,
            password_hashes=wrapped,
        )
    forget()
    logger.info(
        "key ring adopted: %d purposes, %d MFA secrets moved, %d hashes wrapped",
        len(PURPOSES),
        moved,
        wrapped,
    )
    return True


def rotate(
    purpose: str,
    *,
    session=None,
    secret: Optional[str] = None,
    overlap: Optional[timedelta] = None,
    operator: Optional[str] = None,
    older_than: Optional[timedelta] = None,
) -> Optional[int]:
    """A new current key for ``purpose``; the old one verifies for ``overlap``
    (the purpose's own by default, none for a restore or an emergency). The MFA
    key goes decrypt-only and every secret moves under the new one: MFA secrets
    for ``mfa``, every stored password and recovery-code hash for ``pepper``. Returns the
    version, or None when ``older_than`` is set and the current key is younger
    (checked under the lock, so replicas never rotate twice)."""
    spec = PURPOSES[purpose]
    secret = secret or wrapping_secret()
    overlap = spec.overlap if overlap is None else overlap
    with unit_of_work(session) as session:
        _advisory(session)
        row = session.execute(
            text("""
                SELECT version, created_at FROM public.auth_keys
                WHERE purpose = :p AND state = 'current' FOR UPDATE
            """),
            {"p": purpose},
        ).first()
        if row is None:
            raise KeyRingError(f"no current {purpose} key to rotate")
        if older_than is not None and _now() - row[1] < older_than:
            return None
        _read(session, secret)  # a wrong secret must not add a row nobody can open
        # Above every version ever written, retired ones and restore's markers too.
        old = row[0]
        version = session.execute(
            text("SELECT max(version) + 1 FROM public.auth_keys WHERE purpose = :p"),
            {"p": purpose},
        ).scalar_one()
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
        moved = {}
        if spec.encrypts:  # the key just written reads as current
            keys = _read(session, secret)[purpose]
            if purpose == "pepper":
                moved["password_hashes"] = _reencrypt_hashes(session, keys)
            else:
                moved["mfa_secrets"] = _reencrypt_mfa(session, encryptor_from(keys))
        _trail(
            session,
            "auth.key_rotated",
            operator,
            purpose=purpose,
            version=version,
            previous=old,
            **moved,
            overlap_seconds=None if spec.encrypts else int(overlap.total_seconds()),
        )
    forget()
    return version


def _opens(session, secret: str) -> bool:
    try:
        _read(session, secret)
    except KeyRingError:
        return False
    return True


def rewrap(session, old: str, new: str, *, operator=None, reason: str) -> int:
    """Wrap every key still held under ``new`` instead of ``old``; the key
    material does not change. Raises KeyRingError when ``old`` cannot open one."""
    wrap = _wrapper(old)
    rows = session.execute(text("""
            SELECT purpose, version, wrapped FROM public.auth_keys
            WHERE wrapped IS NOT NULL FOR UPDATE
        """)).all()
    # A restored database from before 0060 has no wrapped_at yet.
    stamp = ", wrapped_at = now()" if _has_wrapped_at(session) else ""
    for purpose, version, wrapped in rows:
        try:
            material = wrap.decrypt(wrapped.encode())
        except InvalidToken as exc:
            raise KeyRingError("the key ring is not wrapped under that secret") from exc
        session.execute(
            text(
                f"UPDATE public.auth_keys SET wrapped = :w{stamp} "
                "WHERE purpose = :p AND version = :v"
            ),
            {"w": _wrap(new, material), "p": purpose, "v": version},
        )
    action = "auth.wrapping_key_rotated"
    if reason == "restore":
        action = "auth.key_ring_rewrapped"
    _trail(
        session,
        action,
        operator,
        keys=len(rows),
        reason=reason,
    )
    return len(rows)


def _has_wrapped_at(session) -> bool:
    return session.execute(text("""
                SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                AND table_name = 'auth_keys' AND column_name = 'wrapped_at'
            """)).first() is not None


def _marker(session, purpose: str, version: int) -> None:
    """Retire ``version`` of ``purpose`` here, or record that it was retired
    elsewhere, so a restore never brings it back or reuses its number."""
    done = session.execute(
        text("""
            UPDATE public.auth_keys SET state = 'retired', wrapped = NULL,
                retired_at = now(), verify_until = NULL
            WHERE purpose = :p AND version = :v AND state <> 'retired'
        """),
        {"p": purpose, "v": version},
    ).rowcount
    if done:
        _trail(
            session,
            "auth.key_retired",
            purpose=purpose,
            version=version,
            reason="retired_before_restore",
        )
        return
    session.execute(
        text("""
            INSERT INTO public.auth_keys (purpose, version, state, wrapped, retired_at)
            VALUES (:p, :v, 'retired', NULL, now()) ON CONFLICT DO NOTHING
        """),
        {"p": purpose, "v": version},
    )


def restore_ring(
    session,
    secret: str,
    paired: Optional[str],
    live: dict,
    *,
    operator=None,
) -> bool:
    """Make a restored ring usable under ``secret``, the one this install holds.

    Opens under ``secret``, else under ``paired`` (the backup's own wrapping key)
    and is re-wrapped under ``secret``. If neither opens it, every restored key
    is retired and each purpose gets a fresh one, so no old token verifies.
    ``live`` is what the replaced database knew: ``retired`` (purpose, version)
    pairs and the ``highest`` version per purpose. Those stay retired, and no
    version number is reused. Returns whether the restored ring was kept."""
    opened = _opens(session, secret)
    if not opened and paired and paired != secret and _opens(session, paired):
        rewrap(session, paired, secret, operator=operator, reason="restore")
        opened = True
    if not opened:
        session.execute(text("""
                UPDATE public.auth_keys SET state = 'retired', wrapped = NULL,
                    retired_at = now(), verify_until = NULL WHERE state <> 'retired'
            """))
        _trail(session, "auth.key_ring_reset", operator, reason="restore")
    for purpose, version in live["retired"]:
        _marker(session, purpose, version)
    for purpose, version in live["highest"].items():
        session.execute(
            text("""
                INSERT INTO public.auth_keys
                    (purpose, version, state, wrapped, retired_at)
                SELECT :p, :v, 'retired', NULL, now()
                WHERE :v > coalesce(
                    (SELECT max(version) FROM public.auth_keys WHERE purpose = :p), 0)
            """),
            {"p": purpose, "v": version},
        )
    held = set(
        session.execute(text("SELECT DISTINCT purpose FROM public.auth_keys"))
        .scalars()
        .all()
    )
    current = set(
        session.execute(
            text("SELECT purpose FROM public.auth_keys WHERE state = 'current'")
        )
        .scalars()
        .all()
    )
    for purpose in sorted((held & set(PURPOSES)) - current):
        version = session.execute(
            text("SELECT max(version) + 1 FROM public.auth_keys WHERE purpose = :p"),
            {"p": purpose},
        ).scalar_one()
        _insert(session, secret, purpose, version, "current", _new_material())
    return opened


def _apps_on(session) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM public.users "
            "WHERE mfa_enabled AND mfa_secret IS NOT NULL"
        )
    ).scalar_one()


def clear_unreadable_apps(session, secret: Optional[str], *, everyone=False) -> int:
    """Clear the authenticator secret of each person whose secret no live MFA
    key opens (everyone's, when ``everyone``), so they enrol again. MFA stays on
    for a person with a security key. Returns how many people with the app on lost
    it (counted in the database, so the count never derives from a secret)."""
    keys = [] if everyone else _read(session, secret).get("mfa", [])
    fernet = encryptor_from(keys) if keys else None
    keyed = session.execute(
        text("SELECT to_regclass('public.security_keys') IS NOT NULL")
    ).scalar_one()
    rows = session.execute(
        text("""
            SELECT user_id, principal_id, mfa_secret FROM public.users
            WHERE mfa_secret LIKE :prefix FOR UPDATE
        """),
        {"prefix": "%" if everyone else _FERNET_TOKEN_PREFIX + "%"},
    ).all()
    before = _apps_on(session)
    for user_id, principal, token in rows:
        try:
            if fernet is not None:
                fernet.decrypt(token.encode())
                continue
        except InvalidToken:
            pass
        has_key = (
            keyed
            and session.execute(
                text(
                    "SELECT 1 FROM public.security_keys WHERE principal_id = :p LIMIT 1"
                ),
                {"p": principal},
            ).first()
        )
        session.execute(
            text("""
                UPDATE public.users SET mfa_secret = NULL, mfa_last_totp_step = NULL,
                    mfa_recovery_codes = '[]'::jsonb,
                    mfa_enabled = (mfa_enabled AND :keyed)
                WHERE user_id = :u
            """),
            {"u": user_id, "keyed": bool(has_key)},
        )
    return before - _apps_on(session)


def clear_all_apps(operator: str) -> int:
    """Everyone with an authenticator app enrols again: for the case where the MFA
    key and the database were both exposed. Returns how many people lost the app."""
    with unit_of_work() as session:
        cleared = clear_unreadable_apps(session, None, everyone=True)
        _trail(session, "auth.mfa_reset_all", operator, people=cleared)
    return cleared


def wrapped_since(session=None) -> Optional[datetime]:
    """When the oldest key still held was wrapped: the wrapping secret's age."""
    with unit_of_work(session) as session:
        return session.execute(
            text(
                "SELECT min(wrapped_at) FROM public.auth_keys "
                "WHERE wrapped IS NOT NULL"
            )
        ).scalar_one()


def wrapping_secret_is_stored() -> bool:
    """True when the secret in use is the one in ``secrets.enc``, which Vigil
    can replace. One from the environment or ``.env`` is the operator's."""
    from core.secrets_manager import EncryptedFileBackend, secret_writes_refused

    if secret_writes_refused():
        return False
    backend = EncryptedFileBackend()
    return bool(
        backend.is_available() and backend.get("JWT_SECRET_KEY") == wrapping_secret()
    )


def rotate_wrapping_secret(
    *, operator: Optional[str] = None, older_than: Optional[timedelta] = None
) -> bool:
    """A new JWT_SECRET_KEY in ``secrets.enc``, with the whole ring re-wrapped
    under it in one transaction. Only for a secret that is stored there. With
    ``older_than``, does nothing (False) when another replica just did it."""
    old, new = wrapping_secret(), secrets.token_urlsafe(48)
    try:
        with unit_of_work() as session:
            _advisory(session)
            since = wrapped_since(session)
            if older_than is not None and since and _now() - since < older_than:
                return False
            rewrap(session, old, new, operator=operator, reason="rotation")
            if not set_secret("JWT_SECRET_KEY", new):
                raise KeyRingError("could not write the new secret to the secret store")
    except Exception:
        if get_secret("JWT_SECRET_KEY") == new:  # the store moved, the database did not
            set_secret("JWT_SECRET_KEY", old)
        raise
    forget()
    return True


def inventory() -> list[tuple]:
    """Every key, retired ones too: no key material."""
    with unit_of_work() as session:
        return session.execute(text("""
                SELECT purpose, version, state, created_at, verify_until, retired_at
                FROM public.auth_keys ORDER BY purpose, version
            """)).all()


EMERGENCY_REASON = "emergency key change"


def _stop_runs(operator: str) -> int:
    """Stop every in-flight run through ``stop_run``. This process has no workers
    and ``stop_run``'s escalation timer dies with it, so each also gets its
    terminal now, as a restore does."""
    from core.auth import sign_in_runs
    from core.workflows.run_cancel import force_terminal, stop_run
    from core.workflows.workflow_run_service import WorkflowRunService

    service = WorkflowRunService()
    stopped = 0
    for run_id in sign_in_runs.in_flight_runs():
        try:
            stop_run(run_id, EMERGENCY_REASON, operator)
            sign_in_runs._reject_pending(run_id)
            service.finalize_run(run_id, status="cancelled", error=EMERGENCY_REASON)
            force_terminal(run_id, EMERGENCY_REASON)
            stopped += 1
        except Exception:  # noqa: BLE001 - one run must not keep the rest running
            logger.exception("could not stop run %s", run_id)
    return stopped


def emergency(purposes: list[str], operator: str) -> int:
    """Rotate each purpose with no overlap, stop every running run, and tell the
    holders of ``sessions.manage`` once. Retiring the sign-in key is what signs
    people out. Returns the number of runs stopped."""
    from core.auth import permissions
    from core.auth.mfa_coverage import SETTINGS_LINK
    from core.platform.notices import notify

    for purpose in purposes:
        rotate(purpose, overlap=timedelta(0), operator=operator)
    stopped = _stop_runs(operator)
    with unit_of_work() as session:
        _trail(
            session,
            "auth.emergency_rotation",
            operator,
            purposes=purposes,
            runs_stopped=stopped,
        )
    signed_out = "signin" in purposes
    message = (
        f"Keys were rotated with no overlap in an emergency: {', '.join(purposes)}. "
        + ("Everyone is signed out and signs in again. " if signed_out else "")
        + f"{stopped} agent run{'s' if stopped != 1 else ''} stopped."
    )
    for principal in permissions.holders("sessions.manage"):
        try:
            notify(
                principal,
                "emergency_key_change",
                "Keys were changed in an emergency",
                message,
                link=SETTINGS_LINK,
                priority="high",
            )
        except Exception:  # noqa: BLE001 - the keys are already rotated
            logger.exception("could not tell a session manager about the rotation")
    return stopped


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
    emer = sub.add_parser(
        "emergency",
        help="rotate with no overlap and stop every run (signin signs everyone out)",
    )
    emer.add_argument("purpose", choices=[*sorted(PURPOSES), "all"])
    sub.add_parser(
        "rewrap",
        help="after changing JWT_SECRET_KEY where it is defined: wrap the ring under "
        "the new value (asks for the old one)",
    )
    clear = sub.add_parser(
        "reset-mfa", help="clear every authenticator app, so everyone enrols again"
    )
    clear.add_argument("--yes", action="store_true", help="do it")
    sub.add_parser("status", help="list the keys that are not retired")
    inv = sub.add_parser("inventory", help="list every key, retired ones too")
    inv.add_argument("--json", action="store_true", help="JSON instead of TSV")
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
    if args.command == "inventory":
        fields = ("purpose", "version", "state", "created", "overlap_end", "retired")
        rows = [
            [v.isoformat() if isinstance(v, datetime) else v for v in row]
            for row in inventory()
        ]
        if args.json:
            print(json.dumps([dict(zip(fields, row)) for row in rows], indent=2))
        else:
            print("\t".join(fields))
            for row in rows:
                print("\t".join("" if v is None else str(v) for v in row))
        return 0
    if args.command == "reset-mfa":
        if not args.yes:
            print("this clears every authenticator app; pass --yes", file=sys.stderr)
            return 1
        print(f"{clear_all_apps(getpass.getuser())} people must set up the app again")
        return 0
    try:
        if args.command == "rewrap":
            old = getpass.getpass("Previous JWT_SECRET_KEY: ")
            with unit_of_work() as session:
                _advisory(session)
                new = wrapping_secret()
                if _opens(session, new):
                    raise KeyRingError("the ring is already wrapped under this secret")
                rewrap(session, old, new, operator=getpass.getuser(), reason="rotation")
            print("the key ring is wrapped under the current JWT_SECRET_KEY")
            return 0
        if args.command == "emergency":
            purposes = sorted(PURPOSES) if args.purpose == "all" else [args.purpose]
            stopped = emergency(purposes, getpass.getuser())
            print(
                f"rotated with no overlap: {', '.join(purposes)}; {stopped} run"
                f"{'s' if stopped != 1 else ''} stopped; replicas pick it up within "
                f"{int(REFRESH)} seconds"
            )
            return 0
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
