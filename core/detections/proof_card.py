"""The proof card, exported in open formats a customer can hand to auditors.

One export is one zip, made from one set of data and one signature:

- ``proof-card.json``: the proof as Vigil stores it, in the published
  ``proof-card/v1`` shape. Per rule: its SIEM, techniques, version, a SHA-256
  of its query, every check as stored (result, reason, when) and its path to
  live (its rollout stages, and its repo rule's when it is a linked copy). A
  coverage export holds proven coverage by technique and SIEM over every live
  rule, the count of those, and the rule behind each cell. Its bytes are
  canonical JSON (sorted keys, no spaces), so anyone can re-hash them.
- ``proof-card.json.sig``: an Ed25519 signature over those exact bytes,
  base64.
- ``proof-card.pdf``: the same card for people: it prints the JSON's SHA-256
  and the signature, so a printed copy can be checked against the JSON.
- ``vigil-proof-key.pub``: the instance's public key (PEM). Trust comes from
  getting the key from Vigil itself (GET /api/detection-rules/proof-card/key),
  not from the copy in the zip.

``verify`` (and scripts/vigil_verify.py, which needs only ``cryptography``)
checks a JSON file against its signature and a public key.

The signing key is the instance's own: an Ed25519 key kept in Vigil's secrets
store under ``KEY_SECRET``, made on first export. Where secrets can't be
written (an ephemeral State Directory), set it in the environment instead.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select

from core.auth import trail_events
from core.detections import proof_pdf
from core.detections.inventory.catalog import OVERLAY, tool_of
from core.secrets_manager import get_secret, secret_writes_refused, set_secret
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleStage, DetectionRuleVersion
from core.storage.unit_of_work import unit_of_work

SCHEMA = "proof-card/v1"
KEY_SECRET = "VIGIL_PROOF_SIGNING_KEY"
# The card's exported_from when VIGIL_INSTANCE_NAME is unset: never the host name,
# which would sign a machine's name into every card.
DEFAULT_INSTANCE = "Vigil"
# Trail actions: an export, and the signing key being made.
EXPORTED = "detections.proof_card_exported"
KEY_CREATED = "detections.proof_key_created"
# An ATT&CK technique or sub-technique id. A declared technique that isn't one (a
# template a SIEM never filled, say) stays on its rule but out of the coverage table.
ATTACK_ID = re.compile(r"^T\d{4}(\.\d{3})?$")
# The SIEMs whose live rules a coverage export covers, and Vigil's own overlay.
COVERAGE_TOOLS = ("elastic", "splunk", "wazuh", OVERLAY)
TOOL_NAMES = {
    "elastic": "Elastic",
    "splunk": "Splunk",
    "wazuh": "Wazuh",
    OVERLAY: "Vigil",
}
CHECKS = ("runs", "sees", "quiet", "accurate", "caught", "noisy", "silent", "drift")
CHECK_NAMES = {
    "runs": "Runs",
    "sees": "Sees",
    "quiet": "Quiet",
    "accurate": "Accurate",
    "caught": "Caught",
    "noisy": "Noisy",
    "silent": "Silent",
    "drift": "Drift",
}
# A technique's state in one SIEM, best first: a live rule caught an attack;
# one fires; one can run but never fired; every one fails Runs or Sees.
STATES = ("proven", "fires", "never_fired", "cant_fire")
# The most rule copies a listed export takes. A coverage export has no cap: its
# table reads every live rule and its cards are the rules behind its cells.
MAX_RULES = 2000
# The widest technique column the PDF's coverage table prints.
TECH_W = 24

_key_lock = threading.Lock()


class ProofKeyUnavailable(RuntimeError):
    """No signing key, and none can be stored here."""


# The card ------------------------------------------------------------------


def _result(checks: Dict[str, Any], key: str) -> Optional[str]:
    check = checks.get(key)
    return check.get("result") if isinstance(check, dict) else None


def _state(checks: Dict[str, Any]) -> str:
    if _result(checks, "caught") == "pass":
        return "proven"
    if "fail" in (_result(checks, "runs"), _result(checks, "sees")):
        return "cant_fire"
    silent = checks.get("silent")
    days = silent.get("days_fired") if isinstance(silent, dict) else None
    if isinstance(days, int) and days == 0:
        return "never_fired"
    return "fires"


def coverage(rules: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per technique, each SIEM's best state across its live rules there, and
    the rule behind it (the lowest id at that state)."""
    table: Dict[str, Dict[str, Tuple[str, int]]] = {}
    for rule in sorted(rules, key=lambda r: r["rule_id"]):
        if rule["status"] != "active":
            continue
        state = _state(rule["checks"])
        for technique in rule["techniques"]:
            if not ATTACK_ID.match(technique):
                continue
            cells = table.setdefault(technique, {})
            held = cells.get(rule["siem"])
            if held is None or STATES.index(state) < STATES.index(held[0]):
                cells[rule["siem"]] = (state, rule["rule_id"])
    return [
        {
            "technique": t,
            "siems": {siem: cell[0] for siem, cell in table[t].items()},
            "rule_ids": {siem: cell[1] for siem, cell in table[t].items()},
        }
        for t in sorted(table)
    ]


def unmapped(rules: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Declared techniques that aren't ATT&CK ids, with how many live rules carry each."""
    counts: Dict[str, int] = {}
    for rule in rules:
        if rule["status"] != "active":
            continue
        for technique in set(rule["techniques"]):
            if not ATTACK_ID.match(technique):
                counts[technique] = counts.get(technique, 0) + 1
    return [{"technique": t, "rules": counts[t]} for t in sorted(counts)]


def _stage_row(stage: DetectionRuleStage, of: str) -> Dict[str, Any]:
    return {
        "stage": stage.stage,
        "version": stage.version,
        "at": stage.at.astimezone(timezone.utc).isoformat() if stage.at else None,
        "by": stage.actor,
        "reason": stage.reason,
        "of": of,
    }


def build(
    rule_ids: Optional[List[int]] = None, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The card: the listed rules (any status); or, when ``rule_ids`` is None,
    coverage over every live SIEM and overlay rule with the rules behind it."""
    table: Optional[List[Dict[str, Any]]] = None
    odd: List[Dict[str, Any]] = []
    live = 0
    with get_db_manager().session_scope() as session:
        statement = (
            select(DetectionRule, DetectionRuleVersion)
            .join(
                DetectionRuleVersion,
                (DetectionRuleVersion.rule_id == DetectionRule.id)
                & (DetectionRuleVersion.version == DetectionRule.latest_version),
            )
            .order_by(DetectionRule.id)
        )
        if rule_ids is None:
            statement = statement.where(DetectionRule.status == "active")
        else:
            statement = statement.where(DetectionRule.id.in_(rule_ids[:MAX_RULES]))
        found = session.execute(statement).all()
        if rule_ids is None:
            found = [(r, v) for r, v in found if tool_of(r.source) in COVERAGE_TOOLS]
            live = len(found)
            light = [
                {
                    "rule_id": r.id,
                    "status": r.status,
                    "siem": tool_of(r.source),
                    "techniques": list(r.techniques or []),
                    "checks": v.checks or {},
                }
                for r, v in found
            ]
            table = coverage(light)
            odd = unmapped(light)
            behind = {i for row in table for i in row["rule_ids"].values()}
            found = [(r, v) for r, v in found if r.id in behind]
        wanted = {r.id for r, _ in found} | {
            r.origin_rule_id for r, _ in found if r.origin_rule_id
        }
        stages: Dict[int, List[DetectionRuleStage]] = {}
        if wanted:
            for stage in session.scalars(
                select(DetectionRuleStage)
                .where(DetectionRuleStage.rule_id.in_(wanted))
                .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id)
            ):
                stages.setdefault(stage.rule_id, []).append(stage)
        rules = [_rule(rule, version, stages) for rule, version in found]
    card: Dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "rules" if rule_ids is not None else "coverage",
        "exported_at": (now or datetime.now(timezone.utc)).isoformat(),
        "exported_from": instance_name(),
        "rule_count": len(rules),
        "rules": rules,
    }
    if table is not None:
        card["coverage"] = table
        card["live_rules"] = live
        card["unmapped"] = odd
    return card


def _rule(
    rule: DetectionRule,
    version: DetectionRuleVersion,
    stages: Dict[int, List[DetectionRuleStage]],
) -> Dict[str, Any]:
    checks = version.checks or {}
    path = [_stage_row(s, "this rule") for s in stages.get(rule.id, [])]
    if rule.origin_rule_id:
        path = [
            _stage_row(s, f"its repo rule {rule.origin_rule_id}")
            for s in stages.get(rule.origin_rule_id, [])
        ] + path
    return {
        "rule_id": rule.id,
        "name": rule.name,
        "siem": tool_of(rule.source),
        "native_key": rule.native_key,
        "status": rule.status,
        "techniques": list(rule.techniques or []),
        "version": version.version,
        "first_seen": (
            version.first_seen.astimezone(timezone.utc).isoformat()
            if version.first_seen
            else None
        ),
        "query_sha256": hashlib.sha256((version.query or "").encode()).hexdigest(),
        "checks": {k: checks[k] for k in CHECKS if k in checks},
        "deploy": checks.get("deploy"),
        "path_to_live": path,
    }


def instance_name() -> str:
    """The Vigil the card came from: VIGIL_INSTANCE_NAME, else DEFAULT_INSTANCE."""
    return os.environ.get("VIGIL_INSTANCE_NAME") or DEFAULT_INSTANCE  # noqa: ENV001


def canonical(card: Dict[str, Any]) -> bytes:
    """The card's signed bytes: sorted keys, no spaces, UTF-8."""
    return json.dumps(
        card, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


# The key ---------------------------------------------------------------------


def _private_key(actor: Optional[str] = None) -> Ed25519PrivateKey:
    """The instance's key; made and stored on first use, which the trail records
    as the system's act, triggered by ``actor`` (whoever's request needed it)."""
    with _key_lock:
        stored = get_secret(KEY_SECRET)
        if stored:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(stored))
        if secret_writes_refused():
            raise ProofKeyUnavailable(
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
            raise ProofKeyUnavailable("Could not store a new proof signing key.")
        # The key exists now: recorded, or held to be recorded late. Vigil makes
        # it, whoever's request needed it first; that person is triggered_by.
        trail_events.safety(
            KEY_CREATED,
            actor="system",
            triggered_by=actor,
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
    public = _private_key(actor).public_key()
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


# The export ------------------------------------------------------------------


def sign(
    data: bytes, key: Optional[Ed25519PrivateKey] = None, actor: Optional[str] = None
) -> Tuple[str, str, str]:
    """(signature base64, key id, public key PEM) for ``data``."""
    key = key or _private_key(actor)
    public = key.public_key()
    return base64.b64encode(key.sign(data)).decode(), key_id(public), public_pem(public)


def bundle(card: Dict[str, Any], key: Optional[Ed25519PrivateKey] = None) -> bytes:
    """The zip: the JSON, its signature, the PDF and the public key."""
    return _bundle(card, key)[0]


def export_kind(rule_ids: Optional[List[int]]) -> str:
    """The trail's name for an export: coverage, one rule, or a listed set."""
    if rule_ids is None:
        return "coverage"
    return "single" if len(set(rule_ids)) == 1 else "listed"


def export(
    rule_ids: Optional[List[int]], actor: str, now: Optional[datetime] = None
) -> Tuple[Dict[str, Any], bytes]:
    """(card, zip) for ``actor``, with its trail entry. The entry commits before
    the zip is handed back; if it can't be written the export fails."""
    card = build(rule_ids, now)
    data, digest, kid = _bundle(card, None, actor)
    with unit_of_work() as session:
        trail_events.record(
            session,
            EXPORTED,
            actor=actor,
            kind=export_kind(rule_ids),
            rule_count=card["rule_count"],
            key_id=kid,
            sha256=digest,
        )
    return card, data


def _bundle(
    card: Dict[str, Any],
    key: Optional[Ed25519PrivateKey] = None,
    actor: Optional[str] = None,
) -> Tuple[bytes, str, str]:
    """(zip, SHA-256 of the JSON, key id)."""
    data = canonical(card)
    signature, kid, pem = sign(data, key, actor)
    digest = hashlib.sha256(data).hexdigest()
    pdf = proof_pdf.render(
        _pdf_lines(card, digest, signature, kid), _footer(card, digest)
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proof-card.json", data)
        zf.writestr("proof-card.json.sig", signature + "\n")
        zf.writestr("proof-card.pdf", pdf)
        zf.writestr("vigil-proof-key.pub", pem)
        zf.writestr("README.txt", _readme(digest, kid))
    return out.getvalue(), digest, kid


def _footer(card: Dict[str, Any], digest: str) -> str:
    return f"Vigil proof card · {card['exported_from']} · {card['exported_at'][:19]}Z · data SHA-256 {digest[:16]}…"


def _readme(digest: str, kid: str) -> str:
    return (
        "Vigil proof card\n\n"
        "proof-card.json      the proof, schema proof-card/v1 (canonical JSON)\n"
        "proof-card.json.sig  Ed25519 signature over proof-card.json's exact bytes (base64)\n"
        "proof-card.pdf       the same proof to read and print\n"
        "vigil-proof-key.pub  the signing Vigil's public key (PEM)\n\n"
        f"SHA-256 of proof-card.json: {digest}\n"
        f"Signing key id: {kid}\n\n"
        "exported_from names the Vigil that signed it: its VIGIL_INSTANCE_NAME, or\n"
        f'"{DEFAULT_INSTANCE}" when that is unset (never the machine\'s host name).\n\n'
        "To check it wasn't changed:\n"
        "  python vigil_verify.py proof-card.zip --key <the key you got from Vigil>\n"
        "Get the key from the Vigil itself (GET /api/detection-rules/proof-card/key);\n"
        "the copy in this zip only shows which key signed it.\n"
    )


def _pdf_lines(
    card: Dict[str, Any], digest: str, signature: str, kid: str
) -> List[proof_pdf.Line]:
    lines: List[proof_pdf.Line] = []

    def add(
        text: str, size: float = 9, font: str = "regular", indent: float = 0
    ) -> None:
        lines.append((text, size, font, indent))

    title = "Proven coverage" if card["kind"] == "coverage" else "Detection proof card"
    add("VIGIL PROOF CARD", 8, "bold")
    add(title, 16, "bold")
    count = (
        f"{card['live_rules']} live rules; {card['rule_count']} behind its cells"
        if card["kind"] == "coverage"
        else f"{card['rule_count']} rules"
    )
    add(
        f"Exported {card['exported_at'][:19]}Z from {card['exported_from']} · {count}",
        9,
    )
    add("")
    add("How to check this report", 10, "bold")
    add(f"SHA-256 of proof-card.json: {digest}", 8)
    add(f"Ed25519 signature (key {kid}): {signature}", 8)
    add(
        "This PDF is the readable copy of proof-card.json in the same export. Re-hash that "
        "file and check its signature with the Vigil's public key (vigil_verify.py); a "
        "matching SHA-256 above shows this page reports the same data.",
        8,
    )
    if card.get("coverage"):
        add("")
        add("Coverage by technique and SIEM", 11, "bold")
        add(
            "Proven: a live rule caught a real or replayed attack. Fires: it runs and sees its data. "
            "Never fired: it runs, no alert in its window. Can't fire: every rule fails Runs or Sees. "
            f"Read over all {card['live_rules']} live rules; each cell's rule has its card below.",
            8,
        )
        tools = [
            t
            for t in COVERAGE_TOOLS
            if any(t in row["siems"] for row in card["coverage"])
        ]
        # Courier, so padded columns line up; a technique wider than TECH_W is cut.
        width = min(max(len(r["technique"]) for r in card["coverage"]) + 2, TECH_W)
        add(
            "Technique".ljust(width) + "".join(TOOL_NAMES[t].ljust(13) for t in tools),
            8,
            "mono",
        )
        label = {
            "proven": "Proven",
            "fires": "Fires",
            "never_fired": "Never fired",
            "cant_fire": "Can't fire",
        }
        for row in card["coverage"]:
            cells = "".join(
                label.get(row["siems"].get(t, ""), "-").ljust(13) for t in tools
            )
            technique = row["technique"][: width - 2]
            add(technique.ljust(width) + cells, 8, "mono")
        if card.get("unmapped"):
            odd = ", ".join(
                f"{u['technique']} ({u['rules']})" for u in card["unmapped"]
            )
            add(
                "Declared techniques that aren't ATT&CK ids, left out of the table "
                f"(live rules carrying each): {odd}",
                8,
            )
    for rule in card["rules"]:
        add("")
        add(rule["name"], 10.5, "bold")
        add(
            f"{TOOL_NAMES.get(rule['siem'], rule['siem'])} · {rule['status']} · version {rule['version']}"
            f" · techniques {', '.join(rule['techniques']) or 'none declared'}"
            f" · query SHA-256 {rule['query_sha256'][:16]}…",
            8,
        )
        for key in CHECKS:
            check = rule["checks"].get(key)
            if not isinstance(check, dict):
                continue
            when = check.get("checked_at") or check.get("at") or ""
            reason = str(check.get("reason") or "").replace("\n", " ")[:220]
            parts = [f"{CHECK_NAMES[key]}: {check.get('result', '?')}", when, reason]
            add(" · ".join(p for p in parts if p), 8, "regular", 10)
        if rule["path_to_live"]:
            add("Path to live", 8, "bold", 10)
            for step in rule["path_to_live"]:
                parts = [
                    f"{step['stage']} v{step['version']}",
                    step["at"] or "",
                    f"by {step['by']}",
                    step["reason"] or "",
                ]
                add(
                    " · ".join(p for p in parts if p) + f" ({step['of']})",
                    8,
                    "regular",
                    20,
                )
        deploy = rule.get("deploy")
        if isinstance(deploy, dict) and deploy.get("pr_url"):
            add(f"Deployed by {deploy['pr_url']}", 8, "regular", 10)
    return lines
