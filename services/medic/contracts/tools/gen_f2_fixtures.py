"""Regenerates contracts/fixtures/trust/. Run from contracts/: uv run python tools/gen_f2_fixtures.py

TEST-ONLY keys derived from public seeds. They must never sign anything real:
production keys live in AWS KMS (delegated) and hardware tokens (root), per F2."""

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).parent.parent))
from trust_check import PACK, PACK_DEV, TRUST_ROOT, keyid, pae

OUT = Path("fixtures/trust")
OUT.mkdir(parents=True, exist_ok=True)
NAMES = ["root-1", "root-2", "root-3", "root-4", "pack-a", "pack-b", "dev-1"]


def private(name: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(f"medic-TEST-ONLY-{name}".encode()).digest()
    )


def public_raw(name: str) -> bytes:
    return private(name).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


KID = {n: keyid(public_raw(n)) for n in NAMES}


def key_entry(name: str, not_after: str = "2027-12-31T00:00:00Z") -> dict:
    holder = "yubikey" if name.startswith("root") else "aws-kms"
    return {
        "alg": "ed25519",
        "public": base64.b64encode(public_raw(name)).decode(),
        "not_after": not_after,
        "holder": f"{holder}:test-{name}",
    }


def sign(payload: bytes, ptype: str, signers: list[str]) -> bytes:
    sigs = [
        {
            "keyid": KID[s],
            "sig": base64.b64encode(private(s).sign(pae(ptype, payload))).decode(),
        }
        for s in signers
    ]
    env = {
        "payloadType": ptype,
        "payload": base64.b64encode(payload).decode(),
        "signatures": sigs,
    }
    return (json.dumps(env, indent=2) + "\n").encode()


def root_doc(
    version: int,
    roots: list[str],
    packs: list[str],
    revoked: list[str],
    keys: list[str],
) -> dict:
    return {
        "format": "medic.trust-root/v1",
        "version": version,
        "issued_at": "2026-10-07T00:00:00Z",
        "expires_at": "2027-10-07T00:00:00Z",
        "keys": {KID[k]: key_entry(k) for k in keys},
        "roles": {
            "root": {
                "keyids": [KID[r] for r in roots],
                "threshold": 2,
                "payload_types": [TRUST_ROOT],
            },
            "packs": {
                "keyids": [KID[p] for p in packs],
                "threshold": 1,
                "payload_types": [PACK],
                "channels": ["stable", "beta"],
            },
            "packs-dev": {
                "keyids": [KID["dev-1"]],
                "threshold": 1,
                "payload_types": [PACK_DEV],
                "channels": ["dev"],
                "dev_mode_only": True,
            },
            "fixplans": {
                "keyids": [],
                "threshold": 1,
                "payload_types": ["application/vnd.deeptempo.medic.fixplan.v1+json"],
            },
        },
        "revoked_keyids": [KID[r] for r in revoked],
    }


def dump(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


if __name__ == "__main__":
    v1 = root_doc(
        1,
        ["root-1", "root-2", "root-3"],
        ["pack-a"],
        [],
        ["root-1", "root-2", "root-3", "pack-a", "dev-1"],
    )
    v2 = root_doc(
        2,
        ["root-1", "root-2", "root-3"],
        ["pack-b"],
        ["pack-a"],
        ["root-1", "root-2", "root-3", "pack-b", "dev-1"],
    )
    (OUT / "trust-root-v1.dsse.json").write_bytes(
        sign(dump(v1), TRUST_ROOT, ["root-1", "root-2"])
    )
    (OUT / "trust-root-v2.dsse.json").write_bytes(
        sign(dump(v2), TRUST_ROOT, ["root-1", "root-3"])
    )
    pack = Path("fixtures/packs/sample-0.1.0.medicpack.json").read_bytes()
    (OUT / "sample-0.1.0.dev.dsse.json").write_bytes(sign(pack, PACK_DEV, ["dev-1"]))
    (OUT / "TEST-ONLY-keyids.json").write_text(
        json.dumps(
            {
                "warning": "TEST ONLY. Seeds are public: sha256('medic-TEST-ONLY-<name>').",
                "keyids": KID,
            },
            indent=2,
        )
        + "\n"
    )
    print("wrote", sorted(p.name for p in OUT.iterdir()))
