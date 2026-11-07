"""The proof card export: one zip from one set of data and one signature, with
the signed proof-card/v1 JSON for machines and a PDF of it for people, and a
standalone verifier an auditor can run."""

import base64
import hashlib
import importlib.util
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from core.detections import proof_card
from services.api.routers import detection_rules as routes

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "vigil_verify", ROOT / "scripts" / "vigil_verify.py"
)
vigil_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vigil_verify)

NOW = datetime(2026, 11, 6, 14, 32, 8, tzinfo=timezone.utc)
PASS = {"result": "pass", "reason": "ok", "checked_at": "2026-11-05T23:58:00+00:00"}


def _rule(
    rule_id,
    source="elastic",
    status="active",
    techniques=("T1003.001",),
    origin=None,
    name=None,
):
    return SimpleNamespace(
        id=rule_id,
        name=name or f"Rule {rule_id}",
        source=source,
        native_key=f"key-{rule_id}",
        status=status,
        techniques=list(techniques),
        origin_rule_id=origin,
    )


def _version(rule_id, checks=None, query="process where x"):
    return SimpleNamespace(
        rule_id=rule_id,
        version=4,
        query=query,
        checks=checks if checks is not None else {"runs": PASS, "sees": PASS},
        first_seen=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )


def _stage(rule_id, stage, at, actor="d.lee", reason=""):
    return SimpleNamespace(
        rule_id=rule_id, version=1, stage=stage, at=at, actor=actor, reason=reason
    )


@pytest.fixture
def inventory(monkeypatch):
    state = {"pairs": [], "stages": []}

    class _Session:
        def execute(self, statement):
            return SimpleNamespace(all=lambda: list(state["pairs"]))

        def scalars(self, statement):
            return list(state["stages"])

    @contextmanager
    def scope():
        yield _Session()

    monkeypatch.setattr(
        proof_card, "get_db_manager", lambda: SimpleNamespace(session_scope=scope)
    )
    monkeypatch.setattr(proof_card, "instance_name", lambda: "vigil.lab")
    return state


@pytest.fixture
def key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    monkeypatch.setattr(proof_card, "_private_key", lambda: k)
    return k


# The card


def test_a_rule_carries_its_checks_query_hash_and_path_to_live(inventory):
    checks = {
        "runs": PASS,
        "caught": PASS,
        "deploy": {"pr_url": "https://git/pr/142"},
        "junk": 1,
    }
    inventory["pairs"] = [(_rule(7, origin=3), _version(7, checks))]
    inventory["stages"] = [
        _stage(3, "draft", datetime(2026, 10, 21, tzinfo=timezone.utc), "hunt agent"),
        _stage(
            3,
            "live",
            datetime(2026, 10, 27, tzinfo=timezone.utc),
            "d.lee",
            "caught the replay",
        ),
        _stage(7, "shadow", datetime(2026, 10, 22, tzinfo=timezone.utc)),
    ]
    card = proof_card.build([7], now=NOW)
    (rule,) = card["rules"]
    assert card["schema"] == "proof-card/v1"
    assert card["kind"] == "rules"
    assert "coverage" not in card
    assert rule["siem"] == "elastic"
    assert rule["checks"] == {"runs": PASS, "caught": PASS}
    assert rule["deploy"] == {"pr_url": "https://git/pr/142"}
    assert rule["query_sha256"] == hashlib.sha256(b"process where x").hexdigest()
    assert [(s["stage"], s["of"]) for s in rule["path_to_live"]] == [
        ("draft", "its repo rule 3"),
        ("live", "its repo rule 3"),
        ("shadow", "this rule"),
    ]


def test_a_coverage_export_keeps_siem_and_overlay_rules_only(inventory):
    inventory["pairs"] = [
        (_rule(1, "elastic"), _version(1)),
        (_rule(2, "repo:abc"), _version(2)),
        (_rule(3, "repo:overlay"), _version(3)),
        (_rule(4, "wazuh"), _version(4)),
    ]
    card = proof_card.build(now=NOW)
    assert card["kind"] == "coverage"
    assert [r["rule_id"] for r in card["rules"]] == [1, 3, 4]
    assert card["rule_count"] == 3
    assert card["live_rules"] == 3


def test_a_coverage_export_reads_every_live_rule_and_carries_those_behind_its_cells(
    inventory, monkeypatch
):
    monkeypatch.setattr(proof_card, "MAX_RULES", 2)
    caught = {"runs": PASS, "caught": PASS}
    inventory["pairs"] = [
        (_rule(1, "elastic"), _version(1)),
        (
            _rule(2, "elastic", techniques=("T1003.001", "T1059.001")),
            _version(2, caught),
        ),
        (_rule(3, "elastic", techniques=()), _version(3)),
        (_rule(4, "wazuh"), _version(4)),
        (_rule(5, "wazuh"), _version(5)),
    ]
    card = proof_card.build(now=NOW)
    assert card["live_rules"] == 5
    assert card["coverage"] == [
        {
            "technique": "T1003.001",
            "siems": {"elastic": "proven", "wazuh": "fires"},
            "rule_ids": {"elastic": 2, "wazuh": 4},
        },
        {
            "technique": "T1059.001",
            "siems": {"elastic": "proven"},
            "rule_ids": {"elastic": 2},
        },
    ]
    assert [r["rule_id"] for r in card["rules"]] == [2, 4]


@pytest.mark.parametrize(
    "checks, state",
    [
        ({"caught": {"result": "pass"}, "runs": {"result": "fail"}}, "proven"),
        ({"runs": {"result": "fail"}}, "cant_fire"),
        ({"runs": {"result": "pass"}, "sees": {"result": "fail"}}, "cant_fire"),
        (
            {"runs": {"result": "pass"}, "silent": {"result": "pass", "days_fired": 0}},
            "never_fired",
        ),
        ({"runs": {"result": "pass"}}, "fires"),
    ],
)
def test_a_rules_coverage_state(checks, state):
    assert proof_card._state(checks) == state


def _live(rule_id, siem, techniques, checks, status="active"):
    return {
        "rule_id": rule_id,
        "status": status,
        "siem": siem,
        "techniques": techniques,
        "checks": checks,
    }


def test_coverage_takes_each_siems_best_live_rule():
    fail, caught = {"runs": {"result": "fail"}}, {"caught": {"result": "pass"}}
    rules = [
        _live(1, "elastic", ["T1"], fail),
        _live(2, "elastic", ["T1", "T2"], caught),
        _live(3, "splunk", ["T1"], fail),
        _live(4, "wazuh", ["T1"], caught, status="disabled"),
    ]
    assert proof_card.coverage(rules) == [
        {
            "technique": "T1",
            "siems": {"elastic": "proven", "splunk": "cant_fire"},
            "rule_ids": {"elastic": 2, "splunk": 3},
        },
        {"technique": "T2", "siems": {"elastic": "proven"}, "rule_ids": {"elastic": 2}},
    ]


def test_canonical_bytes_are_sorted_and_compact():
    assert (
        proof_card.canonical({"b": 1, "a": {"d": [1, 2], "c": "é"}})
        == '{"a":{"c":"é","d":[1,2]},"b":1}'.encode()
    )


# The export


def _unzip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_the_zip_holds_json_signature_pdf_and_key_from_one_signature(inventory, key):
    inventory["pairs"] = [(_rule(7), _version(7))]
    card = proof_card.build([7], now=NOW)
    files = _unzip(proof_card.bundle(card))
    assert set(files) == {
        "proof-card.json",
        "proof-card.json.sig",
        "proof-card.pdf",
        "vigil-proof-key.pub",
        "README.txt",
    }
    data, sig = files["proof-card.json"], files["proof-card.json.sig"].decode()
    assert data == proof_card.canonical(card)
    assert proof_card.verify(data, sig, files["vigil-proof-key.pub"].decode())
    digest = hashlib.sha256(data).hexdigest()
    assert files["proof-card.pdf"].startswith(b"%PDF-1.4")
    assert digest.encode() in files["proof-card.pdf"]
    assert sig.strip().encode()[:40] in files["proof-card.pdf"]
    assert digest in files["README.txt"].decode()


def test_a_changed_card_fails_verification(inventory, key):
    inventory["pairs"] = [(_rule(7), _version(7))]
    files = _unzip(proof_card.bundle(proof_card.build([7], now=NOW)))
    card = json.loads(files["proof-card.json"])
    card["rules"][0]["checks"]["runs"]["result"] = "fail"
    pem = files["vigil-proof-key.pub"].decode()
    sig = files["proof-card.json.sig"].decode()
    assert not proof_card.verify(proof_card.canonical(card), sig, pem)
    other = proof_card.public_pem(Ed25519PrivateKey.generate().public_key())
    assert not proof_card.verify(files["proof-card.json"], sig, other)


def test_the_pdf_paginates_a_long_card(inventory, key):
    inventory["pairs"] = [
        (_rule(i), _version(i, {k: PASS for k in proof_card.CHECKS}))
        for i in range(1, 60)
    ]
    files = _unzip(proof_card.bundle(proof_card.build(list(range(1, 60)), now=NOW)))
    pdf = files["proof-card.pdf"]
    assert pdf.count(b"/Type /Page ") > 5
    assert pdf.rstrip().endswith(b"%%EOF")


# The key


def test_the_key_is_made_once_and_stored(monkeypatch):
    store = {}
    monkeypatch.setattr(proof_card, "get_secret", lambda k: store.get(k))
    monkeypatch.setattr(
        proof_card, "set_secret", lambda k, v: store.update({k: v}) or True
    )
    monkeypatch.setattr(proof_card, "secret_writes_refused", lambda: False)
    first = proof_card.public_key()
    assert proof_card.KEY_SECRET in store
    assert proof_card.public_key() == first
    assert len(base64.b64decode(store[proof_card.KEY_SECRET])) == 32


def test_no_key_where_secrets_cant_be_stored(monkeypatch):
    monkeypatch.setattr(proof_card, "get_secret", lambda k: None)
    monkeypatch.setattr(proof_card, "secret_writes_refused", lambda: True)
    with pytest.raises(proof_card.ProofKeyUnavailable):
        proof_card.public_key()


def test_a_key_that_wont_store_is_not_used(monkeypatch):
    monkeypatch.setattr(proof_card, "get_secret", lambda k: None)
    monkeypatch.setattr(proof_card, "secret_writes_refused", lambda: False)
    monkeypatch.setattr(proof_card, "set_secret", lambda k, v: False)
    with pytest.raises(proof_card.ProofKeyUnavailable):
        proof_card.public_key()


# The standalone verifier


def _export(tmp_path, inventory, key):
    inventory["pairs"] = [(_rule(7), _version(7))]
    data = proof_card.bundle(proof_card.build([7], now=NOW))
    (tmp_path / "card.zip").write_bytes(data)
    files = _unzip(data)
    (tmp_path / "key.pub").write_bytes(files["vigil-proof-key.pub"])
    return files


def test_vigil_verify_accepts_the_zip(tmp_path, inventory, key, capsys):
    _export(tmp_path, inventory, key)
    assert (
        vigil_verify.main(
            [str(tmp_path / "card.zip"), "--key", str(tmp_path / "key.pub")]
        )
        == 0
    )
    assert "signature valid" in capsys.readouterr().out


def test_vigil_verify_rejects_a_changed_json(tmp_path, inventory, key, capsys):
    files = _export(tmp_path, inventory, key)
    (tmp_path / "card.json").write_bytes(
        files["proof-card.json"].replace(b'"pass"', b'"fail"', 1)
    )
    (tmp_path / "card.sig").write_bytes(files["proof-card.json.sig"])
    code = vigil_verify.main(
        [
            str(tmp_path / "card.json"),
            "--sig",
            str(tmp_path / "card.sig"),
            "--key",
            str(tmp_path / "key.pub"),
        ]
    )
    assert code == 1
    assert "does not match" in capsys.readouterr().out


def test_vigil_verify_rejects_another_key(tmp_path, inventory, key):
    _export(tmp_path, inventory, key)
    (tmp_path / "other.pub").write_text(
        proof_card.public_pem(Ed25519PrivateKey.generate().public_key())
    )
    assert (
        vigil_verify.main(
            [str(tmp_path / "card.zip"), "--key", str(tmp_path / "other.pub")]
        )
        == 1
    )


# The routes


def test_the_export_route_returns_the_zip(inventory, key):
    inventory["pairs"] = [(_rule(7), _version(7))]
    response = routes.export_proof_card(routes.ProofCardRequest(rule_ids=[7]))
    assert response.media_type == "application/zip"
    assert (
        'filename="vigil-proof-card_rules_' in response.headers["content-disposition"]
    )
    assert "proof-card.json" in _unzip(response.body)


def test_the_routes_say_when_there_is_no_key(inventory, monkeypatch):
    def refuse():
        raise proof_card.ProofKeyUnavailable("no key")

    monkeypatch.setattr(proof_card, "_private_key", refuse)
    for call in (
        lambda: routes.export_proof_card(routes.ProofCardRequest()),
        routes.get_proof_card_key,
    ):
        with pytest.raises(HTTPException) as err:
            call()
        assert err.value.status_code == 503
