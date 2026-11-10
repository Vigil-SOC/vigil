"""Security keys: WebAuthn registration and assertions (#4234, epic #4164).

Library: py_webauthn (``webauthn``, BSD-3-Clause, maintained by Duo). It is the
server-side half only, built on the ``cryptography`` package Vigil already ships,
and takes the allowed algorithm list on both the options and the verification.

The relying-party id is ``VIGIL_CONSOLE_HOSTNAME``, never a request's ``Host``.
Keys are available when it is set and HTTPS is on (the cookie rule,
``auth_cookies._cookie_secure``), or when it is a loopback name. Otherwise the
whole install has no keys, set by configuration alone.

A challenge is a short-lived token signed under the key ring's ``webauthn``
purpose and bound to the account and to what it is for; nothing is stored for a
pending ceremony. A replay is refused by the signature counter and by the last
accepted challenge, which covers authenticators whose counter stays 0.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import jwt
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    decode_credential_public_key,
    parse_attestation_object,
    parse_registration_credential_json,
)
from webauthn.helpers.cose import COSECRV, COSEKTY
from webauthn.helpers.cose import COSEAlgorithmIdentifier as Alg
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
)

from core.auth import key_ring
from core.auth.auth_cookies import _cookie_secure
from core.config import get_settings
from core.storage.models import SecurityKey
from core.time import utcnow

logger = logging.getLogger(__name__)

# What a challenge is for: a key can't answer a ceremony it wasn't asked for.
REGISTER, SIGN_IN, CONFIRM = "register", "sign_in", "confirm"
CHALLENGE_LIFE = timedelta(minutes=2)
RP_NAME = "Vigil"

# FIPS 186-5 signature schemes the library implements (decision 21): ECDSA on
# P-256 and P-521 with SHA-2, RSASSA-PKCS1-v1_5 and PSS with SHA-2 on 2048 bits
# or more, and Ed25519. Never secp256k1, SHA-1 RSA or ML-DSA (FIPS 204). ECDSA
# on P-384 is also in the standard; the library cannot verify it.
RSA_MIN_BITS = 2048
ALLOWED = [
    Alg.ECDSA_SHA_256,
    Alg.ECDSA_SHA_512,
    Alg.EDDSA,
    Alg.RSASSA_PSS_SHA_256,
    Alg.RSASSA_PSS_SHA_384,
    Alg.RSASSA_PSS_SHA_512,
    Alg.RSASSA_PKCS1_v1_5_SHA_256,
    Alg.RSASSA_PKCS1_v1_5_SHA_384,
    Alg.RSASSA_PKCS1_v1_5_SHA_512,
]
_CURVE = {Alg.ECDSA_SHA_256: COSECRV.P256, Alg.ECDSA_SHA_512: COSECRV.P521}

ALGORITHM_REFUSED = (
    "That security key uses a signature algorithm Vigil does not accept. "
    "Use a key that supports ECDSA (P-256), Ed25519 or RSA of 2048 bits or more."
)
NOT_REGISTERED = "That security key could not be registered. Try again."
NOT_ACCEPTED = "That security key didn't work. Try again."


class KeyRefused(Exception):
    """A key step refused; the message is for the person."""


class KeysUnavailable(KeyRefused):
    """This install has no security keys; the message is the reason."""


@dataclass(frozen=True)
class Availability:
    available: bool
    hostname: str
    reason: Optional[str]

    def dump(self) -> dict:
        return {"available": self.available, "reason": self.reason}


def _loopback(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def _is_hostname(host: str) -> bool:
    """A bare DNS name: WebAuthn takes no IP address, port, scheme or path."""
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def availability() -> Availability:
    host = get_settings().vigil_console_hostname.strip().lower()
    if host and _is_hostname(host) and (_loopback(host) or _cookie_secure()):
        return Availability(True, host, None)
    start = "Security keys need HTTPS at a stable hostname; "
    if not host:
        why = start + "no console hostname is set (VIGIL_CONSOLE_HOSTNAME)."
    elif not _is_hostname(host):
        why = start + f"VIGIL_CONSOLE_HOSTNAME must be a bare hostname, not {host}."
    else:
        why = (
            start
            + f"this console is reached at {host}, which is not served over HTTPS."
        )
    return Availability(False, host, why)


def _require() -> str:
    """The relying-party id, or refuse with the install's reason."""
    now = availability()
    if not now.available:
        raise KeysUnavailable(now.reason)
    return now.hostname


def _origin(credential: dict, rp_id: str) -> str:
    """The origin the browser reports, once it is the console's own host: HTTPS
    (HTTP on loopback), any port, since the console may sit behind a proxy."""
    try:
        data = base64url_to_bytes(credential["response"]["clientDataJSON"])
        origin = json.loads(data)["origin"]
        parts = urlsplit(origin)
        ok = parts.hostname == rp_id and (
            parts.scheme == "https" or (parts.scheme == "http" and _loopback(rp_id))
        )
    except Exception as exc:  # noqa: BLE001 - malformed is refused, not raised
        raise KeyRefused(NOT_ACCEPTED) from exc
    if not ok:
        raise KeyRefused(NOT_ACCEPTED)
    return origin


# --- challenges ------------------------------------------------------------------


def _issue(use: str, principal_id: str, challenge: bytes) -> str:
    now = utcnow()
    return key_ring.sign(
        "webauthn",
        {
            "sub": principal_id,
            "use": use,
            "chal": bytes_to_base64url(challenge),
            "iat": now,
            "exp": now + CHALLENGE_LIFE,
        },
    )


def _open(token: Optional[str], use: str, principal_id: str) -> tuple[bytes, int]:
    """The challenge a token carries and when it was issued, if the token is ours,
    for this account and use."""
    try:
        claims = jwt.decode(
            token or "",
            key_ring.token_key("webauthn", token or ""),
            algorithms=["HS256"],
            options={"require": ["exp", "sub", "use", "chal", "iat"]},
        )
    except (jwt.InvalidTokenError, key_ring.KeyRingError) as exc:
        raise KeyRefused(NOT_ACCEPTED) from exc
    if claims["sub"] != principal_id or claims["use"] != use:
        raise KeyRefused(NOT_ACCEPTED)
    return base64url_to_bytes(claims["chal"]), int(claims["iat"])


# --- the person's keys -----------------------------------------------------------


def keys_of(session: Session, principal_id: str) -> list[SecurityKey]:
    return (
        session.query(SecurityKey)
        .filter(SecurityKey.principal_id == principal_id)
        .order_by(SecurityKey.created_at, SecurityKey.key_id)
        .all()
    )


def has_keys(session: Session, principal_id: str) -> bool:
    return (
        session.query(SecurityKey.key_id)
        .filter(SecurityKey.principal_id == principal_id)
        .first()
        is not None
    )


def remove_all(session: Session, principal_id: str) -> int:
    return (
        session.query(SecurityKey)
        .filter(SecurityKey.principal_id == principal_id)
        .delete(synchronize_session=False)
    )


def _descriptors(rows: list[SecurityKey]) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=r.credential_id) for r in rows]


# --- registration ----------------------------------------------------------------


def registration_options(session: Session, user) -> dict:
    """What the browser's ``credentials.create`` takes, with the signed challenge
    to send back. Only the allowed algorithms are offered."""
    rp_id = _require()
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_name=user.username,
        user_display_name=user.full_name or user.username,
        # A stable handle that is not the account's id or name.
        user_id=hashlib.sha256(
            b"vigil-webauthn-user:" + user.principal_id.encode()
        ).digest(),
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED
        ),
        exclude_credentials=_descriptors(keys_of(session, user.principal_id)),
        supported_pub_key_algs=ALLOWED,
    )
    return {
        "options": json.loads(options_to_json(options)),
        "challenge": _issue(REGISTER, user.principal_id, options.challenge),
    }


def _check_key(public_key: bytes) -> int:
    """The COSE algorithm of a public key if it is on the list and the key fits
    it, else refuse."""
    try:
        decoded = decode_credential_public_key(public_key)
        alg = Alg(decoded.alg)
    except Exception as exc:  # noqa: BLE001 - an unknown algorithm number too
        raise KeyRefused(ALGORITHM_REFUSED) from exc
    if alg not in ALLOWED:
        raise KeyRefused(ALGORITHM_REFUSED)
    fits = {
        COSEKTY.EC2: lambda: alg in _CURVE and decoded.crv == _CURVE[alg],
        COSEKTY.OKP: lambda: alg == Alg.EDDSA and decoded.crv == COSECRV.ED25519,
        COSEKTY.RSA: lambda: alg not in _CURVE
        and alg != Alg.EDDSA
        and len(decoded.n.lstrip(b"\x00")) * 8 >= RSA_MIN_BITS,
    }.get(decoded.kty)
    if fits is None or not fits():
        raise KeyRefused(ALGORITHM_REFUSED)
    return int(alg)


def register(
    session: Session, user, name: Optional[str], credential: dict, token: str
) -> SecurityKey:
    """Check the attestation and store the key. Attestation is "none": the
    statement is not checked, only that the key is signed for this site."""
    rp_id = _require()
    challenge, _ = _open(token, REGISTER, user.principal_id)
    try:
        parsed = parse_registration_credential_json(credential)
        attested = parse_attestation_object(
            parsed.response.attestation_object
        ).auth_data.attested_credential_data
        algorithm = _check_key(attested.credential_public_key)
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=_origin(credential, rp_id),
            supported_pub_key_algs=ALLOWED,
        )
    except KeyRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - the library raises several kinds
        logger.info("security key registration refused: %s", exc)
        raise KeyRefused(NOT_REGISTERED) from exc
    taken = (
        session.query(SecurityKey.key_id)
        .filter(SecurityKey.credential_id == verified.credential_id)
        .first()
    )
    if taken:
        raise KeyRefused("That security key is already registered.")
    held = len(keys_of(session, user.principal_id))
    row = SecurityKey(
        key_id=uuid.uuid4().hex,
        principal_id=user.principal_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        algorithm=algorithm,
        sign_count=verified.sign_count,
        name=(name or "").strip()[:100] or f"Security key {held + 1}",
        last_challenge=challenge,
    )
    session.add(row)
    session.flush()
    return row


# --- assertions: sign-in and the strong check ------------------------------------


def assertion_options(session: Session, user, use: str) -> Optional[dict]:
    """Request options naming the person's keys, with the signed challenge, or
    None when they have none. Refuses when keys are unavailable."""
    rp_id = _require()
    rows = keys_of(session, user.principal_id)
    if not rows:
        return None
    options = generate_authentication_options(
        rp_id=rp_id, allow_credentials=_descriptors(rows)
    )
    return {
        "options": json.loads(options_to_json(options)),
        "challenge": _issue(use, user.principal_id, options.challenge),
    }


def verify_assertion(
    session: Session, user, credential: Any, token: Optional[str], use: str
) -> SecurityKey:
    """Accept one assertion of one of the person's keys, once, or raise.

    The key's row is locked so two requests carrying the same assertion are
    serialised: the second sees the counter and challenge the first stored. The
    row changes only when the assertion is good.
    """
    rp_id = _require()
    challenge, issued = _open(token, use, user.principal_id)
    try:
        cred_id = base64url_to_bytes(credential["id"])
    except Exception as exc:  # noqa: BLE001
        raise KeyRefused(NOT_ACCEPTED) from exc
    row = (
        session.query(SecurityKey)
        .filter(
            SecurityKey.principal_id == user.principal_id,
            SecurityKey.credential_id == cred_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    # Refused: the key's last challenge again, or one issued before its last use.
    # The counter says the same for most authenticators; for one that always
    # reports 0 these two are the guard, and the second also stops an older
    # assertion coming back while its challenge is still alive.
    if row is None or row.last_challenge == challenge:
        raise KeyRefused(NOT_ACCEPTED)
    if row.last_used_at and issued < int(
        row.last_used_at.replace(tzinfo=timezone.utc).timestamp()
    ):
        raise KeyRefused(NOT_ACCEPTED)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=_origin(credential, rp_id),
            credential_public_key=row.public_key,
            credential_current_sign_count=row.sign_count,
        )
    except KeyRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - the library raises several kinds
        logger.info("security key assertion refused: %s", exc)
        raise KeyRefused(NOT_ACCEPTED) from exc
    row.sign_count = verified.new_sign_count
    row.last_used_at = utcnow()
    row.last_challenge = challenge
    return row
