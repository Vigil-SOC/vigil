#!/usr/bin/env python3
"""Check a Vigil proof card wasn't changed: its Ed25519 signature and schema.

Needs only Python 3.8+ and the ``cryptography`` package, so an auditor can run
it without Vigil:

    python vigil_verify.py vigil-proof-card.zip --key vigil-proof-key.pub
    python vigil_verify.py proof-card.json --sig proof-card.json.sig --key key.pub

Take the key from the Vigil that signed the card (GET
/api/detection-rules/proof-card/key), not from the zip: the zip's copy only
names the key that signed it. Exit status 0: valid; 1: not valid; 2: unreadable.
"""

import argparse
import base64
import hashlib
import json
import sys
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "proof-card/v1"


def check(data: bytes, signature_b64: str, pem: bytes):
    """(ok, lines to print) for the card's bytes, its signature and a public key."""
    lines = [f"SHA-256 of proof-card.json: {hashlib.sha256(data).hexdigest()}"]
    public = serialization.load_pem_public_key(pem)
    if not isinstance(public, Ed25519PublicKey):
        return False, lines + ["✗ the key is not an Ed25519 public key"]
    try:
        public.verify(base64.b64decode(signature_b64.strip()), data)
    except (InvalidSignature, ValueError):
        return False, lines + [
            "✗ signature does not match: the card was changed, or another key signed it"
        ]
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    lines.append(
        f"✓ signature valid · Ed25519 key {hashlib.sha256(raw).hexdigest()[:16]}"
    )
    try:
        card = json.loads(data)
    except ValueError:
        return False, lines + ["✗ proof-card.json is not JSON"]
    if card.get("schema") != SCHEMA:
        return False, lines + [f"✗ schema is {card.get('schema')!r}, not {SCHEMA}"]
    lines.append(
        f"✓ schema {SCHEMA} · {card.get('rule_count')} rules · exported "
        f"{card.get('exported_at')} from {card.get('exported_from')}"
    )
    return True, lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("card", help="the export's zip, or its proof-card.json")
    parser.add_argument(
        "--sig", help="proof-card.json.sig (read from the zip when omitted)"
    )
    parser.add_argument("--key", required=True, help="the Vigil's public key (PEM)")
    args = parser.parse_args(argv)
    try:
        if zipfile.is_zipfile(args.card):
            with zipfile.ZipFile(args.card) as zf:
                data = zf.read("proof-card.json")
                sig = (
                    open(args.sig).read()
                    if args.sig
                    else zf.read("proof-card.json.sig").decode()
                )
        else:
            data = open(args.card, "rb").read()
            if not args.sig:
                parser.error("--sig is needed with a JSON file")
            sig = open(args.sig).read()
        pem = open(args.key, "rb").read()
    except (OSError, KeyError) as e:
        print(f"✗ could not read the card: {e}", file=sys.stderr)
        return 2
    ok, lines = check(data, sig, pem)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
