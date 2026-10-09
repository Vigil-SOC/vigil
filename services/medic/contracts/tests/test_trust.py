"""F2 contract: DSSE envelopes, trust-root updates, rotation and revocation."""

from __future__ import annotations

import base64
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))
from gen_f2_fixtures import KID, dump, root_doc, sign

from pack_build import build
from pack_check import check_pack
from trust_check import (
    FIXPLAN,
    PACK,
    PACK_DEV,
    TRUST_ROOT,
    check_channel,
    load_root,
    update_trust_root,
    verify_envelope,
)

TRUST = ROOT_DIR / "fixtures" / "trust"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
PACKS = {PACK, PACK_DEV}
V1 = load_root((TRUST / "trust-root-v1.dsse.json").read_bytes(), now=NOW)
SAMPLE = (ROOT_DIR / "fixtures" / "packs" / "sample-0.1.0.medicpack.json").read_bytes()


@pytest.fixture(scope="module")
def stable_pack(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    src = tmp_path_factory.mktemp("stable")
    for p in (ROOT_DIR / "fixtures" / "packs" / "sample-0.1.0").rglob("*"):
        if p.is_file():
            dest = src / p.relative_to(ROOT_DIR / "fixtures" / "packs" / "sample-0.1.0")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(p.read_text())
    (src / "pack.yaml").write_text(
        (src / "pack.yaml").read_text().replace("channel: dev", "channel: stable")
    )
    return build(src)


def v2() -> dict:
    return update_trust_root(
        V1, (TRUST / "trust-root-v2.dsse.json").read_bytes(), now=NOW
    )


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(
        json.loads((ROOT_DIR / "trust.schema.json").read_text())
    )


def test_baked_root_must_carry_its_own_threshold() -> None:
    env = json.loads((TRUST / "trust-root-v1.dsse.json").read_text())
    env["signatures"] = env["signatures"][:1]
    with pytest.raises(ValueError, match="threshold"):
        load_root(json.dumps(env).encode(), now=NOW)


def test_signed_stable_pack_verifies_then_passes_pack_check(stable_pack: bytes) -> None:
    res = verify_envelope(
        sign(stable_pack, PACK, ["pack-a"]), V1, expected=PACKS, now=NOW
    )
    assert res.ok and res.role == "packs" and res.signed_by == [KID["pack-a"]]
    assert res.payload == stable_pack
    assert check_channel(res, V1, "stable") == []
    assert check_pack(res.payload, vigil_version="0.6.0", now=NOW).ok


def test_tampered_payload_is_refused(stable_pack: bytes) -> None:
    env = json.loads(sign(stable_pack, PACK, ["pack-a"]))
    env["payload"] = base64.b64encode(
        stable_pack.replace(b"lane: 2", b"lane: 1", 1)
    ).decode()
    assert verify_envelope(
        json.dumps(env).encode(), V1, expected=PACKS, now=NOW
    ).codes == {"S-SIG"}


def test_payload_type_is_bound_by_the_signature(stable_pack: bytes) -> None:
    env = json.loads(sign(stable_pack, PACK, ["pack-a"]))
    env["payloadType"] = PACK_DEV
    res = verify_envelope(
        json.dumps(env).encode(), V1, expected=PACKS, now=NOW, dev_mode=True
    )
    assert "S-SCOPE" in res.codes


def test_unknown_key_is_out_of_scope(stable_pack: bytes) -> None:
    assert verify_envelope(
        sign(stable_pack, PACK, ["root-4"]), V1, expected=PACKS, now=NOW
    ).codes == {"S-SCOPE"}


def test_unsigned_pack_only_in_dev_mode() -> None:
    assert verify_envelope(SAMPLE, V1, expected=PACKS, now=NOW).codes == {"S-UNSIGNED"}
    res = verify_envelope(SAMPLE, V1, expected=PACKS, now=NOW, dev_mode=True)
    assert res.ok and res.unsigned_dev
    assert (
        check_channel(res, V1, "dev") == [] and check_channel(res, V1, "stable") != []
    )


def test_dev_signed_pack_only_in_dev_mode() -> None:
    data = (TRUST / "sample-0.1.0.dev.dsse.json").read_bytes()
    assert verify_envelope(data, V1, expected=PACKS, now=NOW).codes == {"S-DEV"}
    res = verify_envelope(data, V1, expected=PACKS, now=NOW, dev_mode=True)
    assert res.ok and res.role == "packs-dev" and check_channel(res, V1, "dev") == []


def test_production_key_cannot_sign_a_dev_channel_pack() -> None:
    res = verify_envelope(sign(SAMPLE, PACK, ["pack-a"]), V1, expected=PACKS, now=NOW)
    assert res.ok
    assert [c for c, _ in check_channel(res, V1, "dev")] == ["S-CHANNEL"]


def test_pack_key_cannot_sign_fix_plans_or_trust_roots(stable_pack: bytes) -> None:
    assert verify_envelope(
        sign(b"{}", FIXPLAN, ["pack-a"]), V1, expected={FIXPLAN}, now=NOW
    ).codes == {"S-SCOPE"}
    assert verify_envelope(
        sign(b"{}", FIXPLAN, ["pack-a"]), V1, expected=PACKS, now=NOW
    ).codes == {"S-TYPE"}
    forged = sign(
        dump(
            root_doc(
                9,
                ["root-1", "root-2", "root-3"],
                ["pack-a"],
                [],
                ["root-1", "root-2", "root-3", "pack-a", "dev-1"],
            )
        ),
        TRUST_ROOT,
        ["pack-a"],
    )
    assert "S-ROOT-THRESHOLD" in update_trust_root(V1, forged, now=NOW).codes


def test_rotation_and_revocation(stable_pack: bytes) -> None:
    res = v2()
    assert res.ok, res.errors
    root2 = json.loads(res.payload)
    assert verify_envelope(
        sign(stable_pack, PACK, ["pack-a"]), root2, expected=PACKS, now=NOW
    ).codes == {"S-REVOKED"}
    assert verify_envelope(
        sign(stable_pack, PACK, ["pack-b"]), root2, expected=PACKS, now=NOW
    ).ok


def test_trust_root_needs_root_threshold() -> None:
    env = json.loads((TRUST / "trust-root-v2.dsse.json").read_text())
    env["signatures"] = env["signatures"][:1]
    assert (
        "S-ROOT-THRESHOLD"
        in update_trust_root(V1, json.dumps(env).encode(), now=NOW).codes
    )


def test_trust_root_cannot_be_replayed_or_unrevoked() -> None:
    root2 = json.loads(v2().payload)
    assert update_trust_root(
        root2, (TRUST / "trust-root-v1.dsse.json").read_bytes(), now=NOW
    ).codes >= {"S-ROOT-VERSION"}
    v3 = root_doc(
        3,
        ["root-1", "root-2", "root-3"],
        ["pack-a"],
        [],
        ["root-1", "root-2", "root-3", "pack-a", "dev-1"],
    )
    codes = update_trust_root(
        root2, sign(dump(v3), TRUST_ROOT, ["root-1", "root-2"]), now=NOW
    ).codes
    assert "S-ROOT-UNREVOKE" in codes


def test_root_rotation_needs_old_and_new_thresholds() -> None:
    new_keys = ["root-4", "root-2", "root-3", "pack-a", "dev-1"]
    v2r = root_doc(2, ["root-4", "root-2", "root-3"], ["pack-a"], [], new_keys)
    v2r["roles"]["root"]["threshold"] = 3
    only_old = update_trust_root(
        V1, sign(dump(v2r), TRUST_ROOT, ["root-1", "root-2"]), now=NOW
    )
    assert "S-ROOT-THRESHOLD" in only_old.codes
    both = update_trust_root(
        V1,
        sign(dump(v2r), TRUST_ROOT, ["root-1", "root-2", "root-3", "root-4"]),
        now=NOW,
    )
    assert both.ok, both.errors


def test_expired_delegated_key_and_expired_root(stable_pack: bytes) -> None:
    root = copy.deepcopy(V1)
    root["keys"][KID["pack-a"]]["not_after"] = "2026-10-01T00:00:00Z"
    assert verify_envelope(
        sign(stable_pack, PACK, ["pack-a"]), root, expected=PACKS, now=NOW
    ).codes == {"S-KEY-EXPIRED"}
    late = datetime(2027, 10, 8, tzinfo=UTC)
    assert verify_envelope(
        sign(stable_pack, PACK, ["pack-a"]), V1, expected=PACKS, now=late
    ).codes == {"S-ROOT-EXPIRED"}


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (
            b'{"payloadType":"x","payloadType":"y","payload":"","signatures":[]}',
            "S-JSON",
        ),
        (
            json.dumps(
                {"payloadType": PACK, "payload": "", "signatures": [], "note": 1}
            ).encode(),
            "S-ENVELOPE",
        ),
        (b" " * (6 * 1024 * 1024 + 1), "S-SIZE"),
    ],
    ids=["duplicate-key", "extra-field", "oversize"],
)
def test_malformed_envelopes(data: bytes, code: str) -> None:
    assert verify_envelope(data, V1, expected=PACKS, now=NOW).codes == {code}
