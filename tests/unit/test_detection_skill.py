"""A Detection Skill moves one Vigil rule to another Vigil: its copy per SIEM, its
exceptions and its signed proof card. The import is checked against the
exporting Vigil's key, then saved as a new rule is (lint, replay, sync) and
starts in shadow, whatever the card says it reached. Both directions, and a
refused import, are on the trail as the person.

The round trip runs on a real Postgres when VIGIL_TEST_PG_DSN names a throwaway
database: Vigil A's overlay is one State Directory, Vigil B's another, and the
exported rule is gone from the database before B imports it."""

import base64
import hashlib
import inspect
import io
import json
import os
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from core.detections import detection_skill, overlay, proof_card
from core.detections.inventory import sync
from core.storage.models import DetectionRule, DetectionRuleStage

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")

SPLUNK = """name: Skill probe {k}
id: {k}-s
type: TTP
search: '| tstats count from datamodel=Endpoint.Processes where Processes.process_name=certutil.exe Processes.process="*-urlcache*" by Processes.dest'
mitre_attack_id:
- T1105
"""
ELASTIC = """[metadata]
creation_date = "2026/11/08"

[rule]
rule_id = "{k}-e"
name = "Skill probe {k}"
description = "certutil fetching a file"
type = "eql"
language = "eql"
index = ["logs-endpoint.events.*"]
query = '''process where process.name == "certutil.exe" and process.args : "-urlcache"'''
risk_score = 47
severity = "medium"

[[rule.threat]]
framework = "MITRE ATT&CK"
[[rule.threat.technique]]
id = "T1105"
name = "Ingress Tool Transfer"
"""
SIGMA = """title: Skill probe {k}
id: {k}-g
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    Image|endswith: '\\certutil.exe'
    CommandLine|contains: '-urlcache'
  condition: selection
level: medium
tags:
- attack.t1105
"""
EXCEPTION = {"match": {"host.name": "build-01"}, "note": "the build box fetches tools"}
PROVEN = {
    "Image": "C:\\Windows\\System32\\certutil.exe",
    "CommandLine": "certutil -urlcache -f http://x/a.exe",
}
QUIET = {
    "Image": "C:\\Windows\\System32\\certutil.exe",
    "CommandLine": "certutil -hashfile a.exe",
}


def test_every_overlay_language_has_its_save_argument():
    params = inspect.signature(overlay.save_rule).parameters
    for language in overlay.LAYOUT:
        assert detection_skill.SAVE_ARG[language] in params, language


@pytest.fixture
def trail(monkeypatch):
    """The audit trail, in memory: (action, fields) per entry."""
    rows = []
    for name in ("record", "safety"):
        monkeypatch.setattr(
            f"core.auth.trail_events.{name}",
            (lambda *a, beside=None, **f: rows.append((a[-1], f))),
        )
    monkeypatch.setattr(
        "core.auth.trail_events.refused",
        lambda action, beside=None, **f: rows.append((action, f)),
    )
    return rows


@pytest.fixture
def vigil(monkeypatch, tmp_path, trail):
    """Vigil A on the test database; ``vigil.become_b()`` moves to Vigil B:
    another State Directory (overlay and signing key), A's rule gone."""
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path / "a"))
    key_a = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        proof_card, "_private_key", lambda actor=None, made_by="export": key_a
    )
    monkeypatch.setattr(proof_card, "instance_name", lambda: "vigil-a")
    k = f"skill-{uuid.uuid4().hex[:10]}"

    class Vigil:
        pass

    v = Vigil()
    v.k, v.db, v.trail, v.key_a = k, manager, trail, key_a
    v.pem = proof_card.public_pem(key_a.public_key())

    def become_b():
        ids = [r["rule_id"] for r in v.saved]
        with manager.session_scope() as session:
            for table in (
                "detection_rule_exceptions",
                "detection_rule_check_changes",
                "detection_rule_stages",
                "detection_rule_versions",
            ):
                session.execute(
                    text(f"DELETE FROM {table} WHERE rule_id = ANY(:i)"), {"i": ids}
                )
            session.execute(
                text("DELETE FROM detection_rules WHERE id = ANY(:i)"), {"i": ids}
            )
        monkeypatch.setenv("VIGIL_DIR", str(tmp_path / "b"))
        key_b = Ed25519PrivateKey.generate()
        monkeypatch.setattr(
            proof_card, "_private_key", lambda actor=None, made_by="export": key_b
        )
        monkeypatch.setattr(proof_card, "instance_name", lambda: "vigil-b")

    v.become_b = become_b
    yield v
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@contextmanager
def _as(name):
    from core.integrations.mcp.surface import acting_as

    with acting_as(name):
        yield


def _save(v, exceptions=True):
    with _as("ana"):
        out = overlay.save_rule(
            splunk_yaml=SPLUNK.format(k=v.k),
            elastic_toml=ELASTIC.format(k=v.k),
            sigma_yaml=SIGMA.format(k=v.k),
            proven_events=[PROVEN],
            ruled_out_events=[QUIET],
            exceptions={"elastic": [EXCEPTION]} if exceptions else None,
        )
    assert not out.get("error"), out
    v.saved = out["saved"]
    return out


def _went_live(v, language="elastic"):
    """A's copy reached live, as the card will say."""
    rid = next(s["rule_id"] for s in v.saved if s["source"] == language)
    with v.db.session_scope() as session:
        session.add(
            DetectionRuleStage(
                rule_id=rid,
                version=1,
                stage="live",
                actor="system",
                reason="quiet week",
                at=datetime.now(timezone.utc),
            )
        )
    return rid


def _members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _zip(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return out.getvalue()


def _resigned(members, key, change):
    """The skill with ``change(manifest, card, members)`` applied, signed again by ``key``."""
    manifest = json.loads(members["detection-skill.json"])
    card = json.loads(members["proof-card.json"])
    change(manifest, card, members)
    card_bytes = proof_card.canonical(card)
    manifest["proof_card_sha256"] = hashlib.sha256(card_bytes).hexdigest()
    for item in manifest["copies"]:
        item["sha256"] = hashlib.sha256(members[item["file"]]).hexdigest()
    manifest_bytes = proof_card.canonical(manifest)
    out = dict(members)
    out["proof-card.json"] = card_bytes
    out["proof-card.json.sig"] = base64.b64encode(key.sign(card_bytes)) + b"\n"
    out["detection-skill.json"] = manifest_bytes
    out["detection-skill.json.sig"] = base64.b64encode(key.sign(manifest_bytes)) + b"\n"
    return _zip(out)


def _import(v, data, pem=None, **events):
    return detection_skill.import_skill(
        data, v.pem if pem is None else pem, "user-erin", "erin", **events
    )


def _stage(v, rule_id):
    with v.db.session_scope() as session:
        return session.scalars(
            select(DetectionRuleStage.stage)
            .where(DetectionRuleStage.rule_id == rule_id)
            .order_by(DetectionRuleStage.id.desc())
        ).first()


@pg
def test_a_rule_moves_with_its_copies_exceptions_and_proof_and_starts_in_shadow(vigil):
    v = vigil
    _save(v)
    live_id = _went_live(v)
    manifest, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    files = _members(data)
    assert {c["language"] for c in manifest["copies"]} == {"splunk", "elastic", "sigma"}
    assert {"proof-card.json", "proof-card.pdf", "vigil-proof-key.pub"} <= set(files)
    card = json.loads(files["proof-card.json"])
    assert [
        s["stage"]
        for r in card["rules"]
        if r["rule_id"] == live_id
        for s in r["path_to_live"]
    ][-1] == "live"
    elastic = next(c for c in manifest["copies"] if c["language"] == "elastic")
    assert elastic["exceptions"] == [EXCEPTION]
    assert v.trail[-1][0] == detection_skill.EXPORTED
    assert v.trail[-1][1]["actor"] == "user-ana"

    v.become_b()
    out = _import(v, data)
    assert not out.get("error"), out
    assert out["exported_from"] == "vigil-a"
    by = {s["source"]: s for s in out["saved"]}
    assert set(by) == {"splunk", "elastic", "sigma"}
    # Proven live at A; here it starts where every new rule does.
    # (The Sigma form is no SIEM copy, so it has no stage.)
    staged = [s for s in out["saved"] if s["source"] in sync.STAGED_LANGUAGES]
    assert {s["source"] for s in staged} == {"splunk", "elastic"}
    assert {s["stage"] for s in staged} == {"shadow"}
    assert all(_stage(v, s["rule_id"]) == "shadow" for s in staged)
    assert [e["match"] for e in out["exceptions"]] == [EXCEPTION["match"]]
    assert out["exceptions"][0]["created_by"] == "erin"
    proven = {p["language"]: p for p in out["proven_elsewhere"]}
    assert proven["elastic"]["path_to_live"][-1]["stage"] == "live"
    action, fields = v.trail[-1]
    assert action == detection_skill.IMPORTED
    assert fields["actor"] == "user-erin"
    assert fields["exported_from"] == "vigil-a"
    assert sorted(fields["rule_ids"]) == sorted(s["rule_id"] for s in out["saved"])
    with v.db.session_scope() as session:
        lint = session.execute(
            text(
                "SELECT v.checks->'lint'->>'result' FROM detection_rule_versions v "
                "WHERE v.rule_id = :r"
            ),
            {"r": by["elastic"]["rule_id"]},
        ).scalar()
    assert lint == "pass"


@pg
@pytest.mark.parametrize(
    "tamper, why",
    [
        ("file", "not the file signed"),
        ("manifest", "signature check failed"),
        ("card", "signature check failed"),
        ("other_key", "signature check failed"),
        ("no_key", "public key"),
        ("not_a_zip", "not a Detection Skill zip"),
    ],
)
def test_an_import_that_fails_its_signature_or_hashes_is_refused(vigil, tamper, why):
    v = vigil
    _save(v, exceptions=False)
    _, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    v.become_b()
    members = _members(data)
    pem = None
    elastic = next(n for n in members if n.endswith(".toml"))
    if tamper == "file":
        members[elastic] = members[elastic].replace(b"-urlcache", b"-verifyctl")
    elif tamper == "manifest":
        manifest = json.loads(members["detection-skill.json"])
        manifest["copies"][0]["exceptions"] = [EXCEPTION]
        members["detection-skill.json"] = proof_card.canonical(manifest)
    elif tamper == "card":
        card = json.loads(members["proof-card.json"])
        card["exported_from"] = "someone else"
        members["proof-card.json"] = proof_card.canonical(card)
    elif tamper == "other_key":
        pem = proof_card.public_pem(Ed25519PrivateKey.generate().public_key())
    elif tamper == "no_key":
        pem = " "
    data = b"not a zip" if tamper == "not_a_zip" else _zip(members)
    out = _import(v, data, pem=pem)
    assert why in out["error"]
    assert "saved" not in out
    action, fields = v.trail[-1]
    assert action == detection_skill.IMPORT_REFUSED
    assert fields["actor"] == "user-erin" and why in fields["reason"]
    with v.db.session_scope() as session:
        assert not session.scalars(
            select(DetectionRule.id).where(DetectionRule.native_key.like(f"{v.k}%"))
        ).all()


@pg
def test_a_query_other_than_the_one_the_card_proves_is_refused(vigil):
    # Signed by A's key, but the file is not what A's card proved.
    v = vigil
    _save(v, exceptions=False)
    _, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    v.become_b()
    members = _members(data)
    elastic = next(n for n in members if n.endswith(".toml"))

    def swap(manifest, card, files):
        files[elastic] = files[elastic].replace(b"-urlcache", b"-verifyctl")

    out = _import(v, _resigned(members, _key_of(v), swap))
    assert "is not the one the card proves" in out["error"]


@pg
def test_a_copy_that_fails_lint_here_is_refused(vigil):
    # A skill signed by A whose Splunk copy is keyed to one host: this Vigil's lint refuses it.
    v = vigil
    _save(v, exceptions=False)
    _, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    v.become_b()
    members = _members(data)
    splunk = next(n for n in members if n.startswith("rules/splunk/"))

    def keyed(manifest, card, files):
        files[splunk] = files[splunk].replace(
            b"by Processes.dest", b'Processes.dest="10.20.30.40" by Processes.dest'
        )
        record = overlay._parse("splunk", files[splunk].decode())
        rid = next(
            c["rule_id"] for c in manifest["copies"] if c["language"] == "splunk"
        )
        for rule in card["rules"]:
            if rule["rule_id"] == rid:
                rule["query_sha256"] = hashlib.sha256(record.query.encode()).hexdigest()

    out = _import(v, _resigned(members, _key_of(v), keyed))
    assert "fails lint" in out["error"]
    assert v.trail[-1][0] == detection_skill.IMPORT_REFUSED


@pg
def test_the_sigma_form_is_replayed_on_the_events_the_importer_gives(vigil):
    v = vigil
    _save(v, exceptions=False)
    _, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    v.become_b()
    refused = _import(v, data, ruled_out_events=[PROVEN])
    assert "matched a ruled-out event" in refused["error"]
    out = _import(v, data, proven_events=[PROVEN], ruled_out_events=[QUIET])
    sigma = next(s for s in out["saved"] if s["source"] == "sigma")
    with v.db.session_scope() as session:
        replay = session.execute(
            text(
                "SELECT checks->'replay'->>'result' FROM detection_rule_versions WHERE rule_id=:r"
            ),
            {"r": sigma["rule_id"]},
        ).scalar()
    assert replay == "pass"


@pg
def test_an_import_never_changes_a_rule_already_here(vigil):
    v = vigil
    _save(v, exceptions=False)
    _, data = detection_skill.export(v.saved[0]["rule_id"], actor="user-ana")
    out = _import(v, data)  # still on A: the copies are here
    assert "already holds" in out["error"]


@pg
def test_only_vigils_own_rules_export(vigil):
    v = vigil
    with v.db.session_scope() as session:
        rule = DetectionRule(
            source="elastic",
            native_key=f"{v.k}-kibana",
            name="SIEM rule",
            language="elastic",
            latest_version=1,
        )
        session.add(rule)
        session.flush()
        rid = rule.id
    try:
        with pytest.raises(detection_skill.Refused, match="Only Vigil's own rules"):
            detection_skill.export(rid, actor="user-ana")
    finally:
        with v.db.session_scope() as session:
            session.execute(text("DELETE FROM detection_rules WHERE id=:i"), {"i": rid})


def _key_of(v):
    return v.key_a
