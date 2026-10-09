"""Reference verifier for Medic signing (F2): DSSE envelopes and trust roots.

Fully offline: Ed25519 verification against a trust root baked into the Medic image
and updated only by a newer trust root signed by the current root role. No network,
no transparency log, no OCSP, so it works the same on air-gapped installs.

The pack loader (F6) calls verify_envelope() on the raw import bytes FIRST. Only the
small, fixed-shape envelope is parsed before verification; the payload (the pack)
is not parsed until a signature has verified. Then it runs pack_check on the payload.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator

HERE = Path(__file__).parent
_SCHEMA = json.loads((HERE / "trust.schema.json").read_text())
ENVELOPE = Draft202012Validator(_SCHEMA)
ROOT = Draft202012Validator(
    {
        "$schema": _SCHEMA["$schema"],
        "$defs": _SCHEMA["$defs"],
        "$ref": "#/$defs/trust_root",
    }
)
MAX_ENVELOPE_BYTES = 6 * 1024 * 1024  # a 4 MB pack, base64-encoded, plus signatures
MAX_ROOT_LIFETIME_DAYS = 400

PACK = "application/vnd.deeptempo.medic.pack.v1+json"
PACK_DEV = "application/vnd.deeptempo.medic.pack-dev.v1+json"
TRUST_ROOT = "application/vnd.deeptempo.medic.trust-root.v1+json"
FIXPLAN = "application/vnd.deeptempo.medic.fixplan.v1+json"


@dataclass
class Verified:
    errors: list[tuple[str, str]] = field(default_factory=list)
    payload: bytes | None = None
    payload_type: str | None = None
    role: str | None = None
    signed_by: list[str] = field(default_factory=list)
    unsigned_dev: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> set[str]:
        return {c for c, _ in self.errors}


def keyid(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:32]


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding: binds the type to the bytes."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _no_dupes(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON keys")
    return dict(pairs)


def _strict_json(data: bytes):
    def bad_constant(name: str) -> None:
        raise ValueError(f"non-standard constant {name}")

    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=bad_constant
    )


def _role_for(root: dict, payload_type: str) -> tuple[str, dict] | None:
    for name in ("packs", "packs-dev", "fixplans", "root"):
        role = root["roles"][name]
        if payload_type in role["payload_types"]:
            return name, role
    return None


def _check_sigs(
    env: dict, payload: bytes, root: dict, role: dict, now: datetime
) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (distinct valid keyids, per-signature failure reasons)."""
    message = pae(env["payloadType"], payload)
    good: list[str] = []
    reasons: list[tuple[str, str]] = []
    for s in env["signatures"]:
        kid = s["keyid"]
        if kid in root["revoked_keyids"]:
            reasons.append(("S-REVOKED", f"key {kid} is revoked"))
            continue
        if kid not in role["keyids"] or kid not in root["keys"]:
            reasons.append(("S-SCOPE", f"key {kid} may not sign {env['payloadType']}"))
            continue
        key = root["keys"][kid]
        if _ts(key["not_after"]) <= now:
            reasons.append(
                ("S-KEY-EXPIRED", f"key {kid} expired at {key['not_after']}")
            )
            continue
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key["public"])).verify(
                base64.b64decode(s["sig"]), message
            )
        except (InvalidSignature, ValueError):
            reasons.append(("S-SIG", f"signature by {kid} doesn't verify"))
            continue
        if kid not in good:
            good.append(kid)
    return good, reasons


def verify_envelope(
    data: bytes,
    root: dict,
    *,
    expected: set[str],
    now: datetime,
    dev_mode: bool = False,
) -> Verified:
    """Verify an import before anything parses its payload.

    expected: payload types the caller accepts here (a pack import passes {PACK, PACK_DEV}).
    """
    res = Verified()
    err = res.errors.append
    if _ts(root["expires_at"]) <= now:
        err(
            (
                "S-ROOT-EXPIRED",
                f"trust root v{root['version']} expired; import a newer one or upgrade Vigil",
            )
        )
        return res
    if len(data) > MAX_ENVELOPE_BYTES:
        err(("S-SIZE", f"{len(data)} bytes"))
        return res
    try:
        env = _strict_json(data)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        err(("S-JSON", str(exc)[:120]))
        return res
    if isinstance(env, dict) and "format" in env and "payloadType" not in env:
        # A bare, unsigned pack.
        if dev_mode and PACK_DEV in expected:
            res.payload, res.payload_type, res.role, res.unsigned_dev = (
                data,
                PACK_DEV,
                "unsigned",
                True,
            )
            return res
        err(("S-UNSIGNED", "unsigned content is refused outside dev mode"))
        return res
    errors = list(ENVELOPE.iter_errors(env))
    if errors:
        err(("S-ENVELOPE", errors[0].message[:150]))
        return res
    ptype = env["payloadType"]
    if ptype not in expected:
        err(("S-TYPE", f"{ptype} is not accepted here"))
        return res
    found = _role_for(root, ptype)
    if found is None or not found[1]["keyids"]:
        err(("S-SCOPE", f"no key may sign {ptype} under trust root v{root['version']}"))
        return res
    name, role = found
    if role.get("dev_mode_only") and not dev_mode:
        err(("S-DEV", f"{ptype} is accepted only in dev mode"))
        return res
    payload = base64.b64decode(env["payload"])
    good, reasons = _check_sigs(env, payload, root, role, now)
    if len(good) < role["threshold"]:
        res.errors.extend(reasons)
        if good or not reasons:
            err(
                (
                    "S-THRESHOLD",
                    f"{len(good)} of {role['threshold']} required signatures",
                )
            )
        return res
    res.payload, res.payload_type, res.role, res.signed_by = payload, ptype, name, good
    return res


def update_trust_root(current: dict, data: bytes, *, now: datetime) -> Verified:
    """Accept a newer trust root only if the current root role AND (when it changed)
    the new root role each sign it with their threshold, the version goes up,
    and no revoked key is un-revoked."""
    res = verify_envelope(data, current, expected={TRUST_ROOT}, now=now)
    if not res.ok:
        if res.codes & {
            "S-SIG",
            "S-THRESHOLD",
            "S-SCOPE",
            "S-REVOKED",
            "S-KEY-EXPIRED",
        }:
            res.errors.append(
                ("S-ROOT-THRESHOLD", "not signed by the current root role's threshold")
            )
        return res
    try:
        new = _strict_json(res.payload)
    except (ValueError, UnicodeDecodeError) as exc:
        return Verified(errors=[("S-JSON", str(exc)[:120])])
    errors = list(ROOT.iter_errors(new))
    if errors:
        return Verified(errors=[("S-ROOT-SCHEMA", errors[0].message[:150])])
    out = Verified(
        payload=res.payload,
        payload_type=TRUST_ROOT,
        role="root",
        signed_by=res.signed_by,
    )
    if new["version"] <= current["version"]:
        out.errors.append(
            (
                "S-ROOT-VERSION",
                f"v{new['version']} is not newer than v{current['version']}",
            )
        )
    if not set(current["revoked_keyids"]) <= set(new["revoked_keyids"]):
        out.errors.append(("S-ROOT-UNREVOKE", "a revoked key can never be un-revoked"))
    issued, expires = _ts(new["issued_at"]), _ts(new["expires_at"])
    if (
        not issued < expires
        or (expires - issued).days > MAX_ROOT_LIFETIME_DAYS
        or expires <= now
    ):
        out.errors.append(
            ("S-ROOT-EXPIRED", "trust root lifetime invalid or already expired")
        )
    if set(new["roles"]["root"]["keyids"]) != set(current["roles"]["root"]["keyids"]):
        env = json.loads(data)
        good, _ = _check_sigs(env, res.payload, new, new["roles"]["root"], now)
        if len(good) < new["roles"]["root"]["threshold"]:
            out.errors.append(
                (
                    "S-ROOT-THRESHOLD",
                    "a root rotation must also be signed by the new root role's threshold",
                )
            )
    return out


def load_root(data: bytes, *, now: datetime) -> dict:
    """Load the trust root baked into the image. Like a TUF root, it must carry its
    own root role's threshold of signatures (checked here, not just at build time)."""
    env = _strict_json(data)
    if list(ENVELOPE.iter_errors(env)) or env["payloadType"] != TRUST_ROOT:
        raise ValueError("not a trust-root envelope")
    payload = base64.b64decode(env["payload"])
    root = _strict_json(payload)
    errors = list(ROOT.iter_errors(root))
    if errors:
        raise ValueError(errors[0].message)
    good, _ = _check_sigs(env, payload, root, root["roles"]["root"], now)
    if len(good) < root["roles"]["root"]["threshold"]:
        raise ValueError("trust root is not signed by its own root role's threshold")
    return root


def check_channel(
    verified: Verified, root: dict, pack_channel: str
) -> list[tuple[str, str]]:
    """F6 calls this after parsing the verified pack: the signer's role must cover its channel."""
    if verified.unsigned_dev:
        return (
            []
            if pack_channel == "dev"
            else [("S-CHANNEL", "unsigned packs may only be dev-channel")]
        )
    allowed = root["roles"][verified.role].get("channels", [])
    return (
        []
        if pack_channel in allowed
        else [("S-CHANNEL", f"role {verified.role} may not sign {pack_channel} packs")]
    )
