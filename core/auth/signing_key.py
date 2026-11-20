"""The instance's signing key: one Ed25519 key that signs what this Vigil exports.

Proof cards and Detection Skill manifests are signed with it, and anyone holding
the public key can check them without Vigil (scripts/vigil_verify.py needs only
``cryptography``). Trust comes from getting the public key from the Vigil
itself, never from a copy inside an export.

The key is kept in Vigil's secrets store under ``KEY_SECRET``, made on first
use. Where secrets can't be written (an ephemeral State Directory), set it in
the environment instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.auth import trail_events
from core.secrets_manager import get_secret, secret_writes_refused, set_secret

KEY_SECRET = "VIGIL_PROOF_SIGNING_KEY"
# The trail action for the key being made. Its stored name predates the key
# being shared beyond proof cards, and entries already written carry it.
KEY_CREATED = "detections.proof_key_created"
# What a request that made the key was: an export, or a read of the public key.
MADE_BY = ("export", "key_read")

_key_lock = threading.Lock()


class SigningKeyUnavailable(RuntimeError):
    """No signing key, and none can be stored here."""


def canonical(document: Dict[str, Any]) -> bytes:
    """A document's signed bytes: sorted keys, no spaces, UTF-8."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def _private_key(
    actor: Optional[str] = None, made_by: str = "export"
) -> Ed25519PrivateKey:
    """The instance's key; made and stored on first use, which the trail records
    as the system's act on behalf of ``actor``, whose request needed it, and
    ``made_by`` that request (one of MADE_BY). The person is named by id in
    ``on_behalf_of``, which the trail resolves to their name when it is read
    (and blanks if they are erased); no name is written into the entry."""
    with _key_lock:
        stored = get_secret(KEY_SECRET)
        if stored:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(stored))
        if secret_writes_refused():
            raise SigningKeyUnavailable(
                f"No proof signing key: this Vigil can't store one. Set {KEY_SECRET} "
                "(a base64 Ed25519 private key) in its environment."
            )
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        if not set_secret(KEY_SECRET, base64.b64encode(raw).decode()):
            raise SigningKeyUnavailable("Could not store a new proof signing key.")
        # The key exists now: recorded, or held to be recorded late. Vigil makes
        # it, whoever's request needed it first; that person is triggered_by.
        trail_events.safety(
            KEY_CREATED,
            actor="system",
            on_behalf_of=actor,
            triggered_by=actor,
            made_by=made_by,
            key_id=key_id(key.public_key()),
        )
        return key


def public_pem(public: Ed25519PublicKey) -> str:
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def key_id(public: Ed25519PublicKey) -> str:
    """A short name for a public key: the first 16 hex of its raw bytes' SHA-256."""
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def public_key(actor: Optional[str] = None) -> Dict[str, str]:
    public = _private_key(actor, "key_read").public_key()
    return {"key_id": key_id(public), "public_key_pem": public_pem(public)}


def verify(data: bytes, signature_b64: str, pem: str) -> bool:
    """Whether ``signature_b64`` is the key's Ed25519 signature over ``data``."""
    public = serialization.load_pem_public_key(pem.encode())
    if not isinstance(public, Ed25519PublicKey):
        return False
    try:
        public.verify(base64.b64decode(signature_b64), data)
    except (InvalidSignature, ValueError):
        return False
    return True


def sign(
    data: bytes, key: Optional[Ed25519PrivateKey] = None, actor: Optional[str] = None
) -> Tuple[str, str, str]:
    """(signature base64, key id, public key PEM) for ``data``."""
    key = key or _private_key(actor)
    public = key.public_key()
    return base64.b64encode(key.sign(data)).decode(), key_id(public), public_pem(public)
