"""A Detection Skill: one Vigil rule, moved to another Vigil with its proof.

An export is one zip (the folder, as one file) from the rule's overlay copies:

- ``rules/<language>/<file>``: each copy's file as the overlay holds it, one per
  SIEM language (and its Sigma form).
- ``detection-skill.json``: the manifest, canonical JSON: the rule's name, each
  copy's language, id, file and SHA-256, and its active exceptions (match and
  note); the SHA-256 of the proof card; where and when it was exported.
- ``detection-skill.json.sig``: the instance's Ed25519 signature over the
  manifest's exact bytes, with the proof card's key.
- ``proof-card.json``, ``.sig``, ``.pdf`` and ``vigil-proof-key.pub``: the
  signed proof card (proof-card/v1) of the copies and the SIEM copies linked
  to them: what was proven, where, and the path each took to live.

An import trusts nothing in the zip but what the exporting Vigil's public key
signed: the person gives that key (GET /api/detection-rules/proof-card/key on
the exporting Vigil). It is refused, with the reason, when either signature or
a file's hash fails, when a copy's query is not the one the card proves, when
this Vigil already holds a copy's id, or when a copy fails what any new rule
fails (lint, a refused replay). Otherwise the copies are saved as a new rule
is (``overlay.save_rule``): linted, replayed on the evidence given, synced,
and in shadow. Nothing an export says moves the rule past shadow here.

The trail records each export, import and refused import as the person.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import and_, select

from core.auth import trail_events
from core.detections import exceptions as excepts
from core.detections import overlay, proof_card
from core.detections.detection_rules_service import DetectionRulesService
from core.detections.inventory import readers, sync
from core.integrations.mcp.surface import acting_as
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

SCHEMA = "detection-skill/v1"
MANIFEST = "detection-skill.json"
EXPORTED = "detections.skill_exported"
IMPORTED = "detections.skill_imported"
IMPORT_REFUSED = "detections.skill_import_refused"
# save_rule's argument for each copy language it takes.
SAVE_ARG = {
    "splunk": "splunk_yaml",
    "elastic": "elastic_toml",
    "sigma": "sigma_yaml",
    "wazuh": "wazuh_xml",
}
# What an import reads, at most: the zip, its entries, and any one entry.
MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 32
MAX_ENTRY_BYTES = 2 * 1024 * 1024


class Refused(ValueError):
    """An export or import that can't go ahead; the message says why."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The export ------------------------------------------------------------------


def _copies(rule_id: int) -> Tuple[str, List[DetectionRule], List[int]]:
    """(name, the rule's active overlay copies, every rule id its card covers).

    ``rule_id`` is any of the rule's overlay copies, or a SIEM copy linked to
    one. The overlay saves one rule's copies under one name; the SIEM copies
    linked to them carry the proof the rule earned after it was deployed."""
    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None:
            raise LookupError(f"No rule {rule_id}")
        if rule.source != readers.OVERLAY_SOURCE and rule.origin_rule_id:
            origin = session.get(DetectionRule, rule.origin_rule_id)
            if origin is not None and origin.source == readers.OVERLAY_SOURCE:
                rule = origin
        if rule.source != readers.OVERLAY_SOURCE:
            raise Refused(
                "Only Vigil's own rules export as a Detection Skill: this one is a "
                "SIEM's or a repo's, which travels through that SIEM or repo."
            )
        copies = list(
            session.scalars(
                select(DetectionRule)
                .where(
                    DetectionRule.source == readers.OVERLAY_SOURCE,
                    DetectionRule.name == rule.name,
                    DetectionRule.status == "active",
                )
                .order_by(DetectionRule.id)
            )
        )
        if not copies:
            raise Refused(f"Rule {rule.name!r} has no active copy to export")
        ids = [c.id for c in copies]
        linked = session.scalars(
            select(DetectionRule.id)
            .where(
                DetectionRule.origin_rule_id.in_(ids),
                DetectionRule.link_kind != "declined",
            )
            .order_by(DetectionRule.id)
        ).all()
        session.expunge_all()
    return rule.name, copies, ids + [i for i in linked if i not in ids]


def _file(root: str, copy: DetectionRule, card: Dict[str, Any]) -> bytes:
    """The copy's overlay file; Refused unless it reads as the version the card proves."""
    if copy.language not in overlay.LAYOUT or not copy.path:
        raise Refused(f"copy {copy.native_key} has no overlay file")
    data = Path(DetectionRulesService._under(root, copy.path)).read_bytes()
    record = overlay._parse(copy.language, data.decode("utf-8"))
    proven = next((r for r in card["rules"] if r["rule_id"] == copy.id), None)
    if proven is None:
        raise Refused(f"copy {copy.native_key} has no stored version to prove")
    if _sha(record.query.encode()) != proven["query_sha256"]:
        raise Refused(
            f"{copy.path} has changed since the overlay last synced it: sync, then export"
        )
    return data


def export(rule_id: int, actor: str) -> Tuple[Dict[str, Any], bytes]:
    """(manifest, zip) for ``actor``, with its trail entry; the entry commits
    before the zip is handed back, and if it can't be written the export fails."""
    name, copies, card_ids = _copies(rule_id)
    card = proof_card.build(card_ids)
    card_bytes = proof_card.canonical(card)
    root = DetectionRulesService().overlay_source()["local_path"]
    files: Dict[str, bytes] = {}
    listed = []
    with get_db_manager().session_scope() as session:
        for copy in copies:
            data = _file(root, copy, card)
            arcname = f"rules/{copy.path}"
            files[arcname] = data
            listed.append(
                {
                    "language": copy.language,
                    "native_key": copy.native_key,
                    "rule_id": copy.id,
                    "file": arcname,
                    "sha256": _sha(data),
                    "exceptions": [
                        {"match": e["match"], "note": e["note"]}
                        for e in excepts.listed(session, copy.id)
                    ],
                }
            )
    manifest = {
        "schema": SCHEMA,
        "name": name,
        "exported_at": card["exported_at"],
        "exported_from": card["exported_from"],
        "proof_card_sha256": _sha(card_bytes),
        "copies": listed,
    }
    manifest_bytes = proof_card.canonical(manifest)
    signature, kid, pem = proof_card.sign(manifest_bytes, actor=actor)
    card_zip = proof_card.bundle(card)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST, manifest_bytes)
        zf.writestr(f"{MANIFEST}.sig", signature + "\n")
        with zipfile.ZipFile(io.BytesIO(card_zip)) as cz:
            for member in cz.namelist():
                if member != "README.txt":
                    zf.writestr(member, cz.read(member))
        for arcname, data in files.items():
            zf.writestr(arcname, data)
        zf.writestr("README.txt", _readme(name, _sha(manifest_bytes), kid))
    with unit_of_work() as session:
        trail_events.record(
            session,
            EXPORTED,
            actor=actor,
            name=name,
            rule_ids=[c.id for c in copies],
            languages=[c.language for c in copies],
            key_id=kid,
            sha256=_sha(manifest_bytes),
            proof_card_sha256=manifest["proof_card_sha256"],
        )
    return manifest, out.getvalue()


def _readme(name: str, digest: str, kid: str) -> str:
    return (
        f"Vigil Detection Skill: {name}\n\n"
        "detection-skill.json      the rule's copies, their SHA-256s and exceptions\n"
        "detection-skill.json.sig  Ed25519 signature over detection-skill.json (base64)\n"
        "rules/                    each copy's file, one per SIEM language\n"
        "proof-card.json (.sig)    what the rule proved, and where (proof-card/v1)\n"
        "proof-card.pdf            the same proof to read and print\n"
        "vigil-proof-key.pub       the exporting Vigil's public key (PEM)\n\n"
        f"SHA-256 of detection-skill.json: {digest}\n"
        f"Signing key id: {kid}\n\n"
        "Import it into another Vigil with this Vigil's public key, got from this\n"
        "Vigil itself (GET /api/detection-rules/proof-card/key), not from the copy\n"
        "here. The imported rule is linted, replayed and checked as a new rule, and\n"
        "starts in shadow.\n"
    )


# The import ------------------------------------------------------------------


def _entries(data: bytes) -> Dict[str, bytes]:
    """Every entry of the zip, bounded; Refused when it isn't one we can read."""
    if len(data) > MAX_ZIP_BYTES:
        raise Refused(f"The file is over {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise Refused("The file is not a Detection Skill zip") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise Refused(f"The zip holds over {MAX_ENTRIES} files")
        out = {}
        for info in infos:
            if info.file_size > MAX_ENTRY_BYTES:
                raise Refused(f"{info.filename} is too large")
            try:
                with zf.open(info) as f:
                    body = f.read(MAX_ENTRY_BYTES + 1)
            # A damaged entry (bad CRC, a size that lies, a broken stream) or one
            # this Python can't decompress.
            except (
                zipfile.BadZipFile,
                zlib.error,
                EOFError,
                NotImplementedError,
            ) as exc:
                raise Refused(f"{info.filename} in the zip is damaged: {exc}") from exc
            if len(body) > MAX_ENTRY_BYTES:
                raise Refused(f"{info.filename} is too large")
            out[info.filename] = body
    return out


def _key_id(pem: str) -> str:
    """The key id proof cards name a key by; Refused when ``pem`` isn't an Ed25519 key."""
    try:
        public = serialization.load_pem_public_key(pem.encode())
    except (ValueError, TypeError) as exc:
        raise Refused(f"The public key could not be read: {exc}") from exc
    if not isinstance(public, Ed25519PublicKey):
        raise Refused("The public key is not an Ed25519 key")
    return proof_card.key_id(public)


def _signed(entries: Dict[str, bytes], name: str, pem: str) -> bytes:
    """``name``'s bytes; Refused unless its .sig is the key's signature over them."""
    data, sig = entries.get(name), entries.get(f"{name}.sig")
    if data is None or sig is None:
        raise Refused(f"The zip has no {name} with its signature")
    try:
        good = proof_card.verify(data, sig.decode().strip(), pem)
    except (ValueError, TypeError, UnicodeDecodeError):
        good = False
    if not good:
        raise Refused(
            f"{name}'s signature check failed: it was changed after it was signed, "
            "or the key given is not the exporting Vigil's"
        )
    return data


def _load(data: bytes, what: str) -> Dict[str, Any]:
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused(f"{what} is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise Refused(f"{what} is not a JSON object")
    return doc


def read(data: bytes, public_key_pem: str) -> Dict[str, Any]:
    """The skill's verified parts: name, the card, and per copy its language,
    text, exceptions and the card's entry for it. Refused on any check that fails."""
    if not (public_key_pem or "").strip():
        raise Refused(
            "Give the exporting Vigil's public key (GET "
            "/api/detection-rules/proof-card/key there) to check the signatures"
        )
    _key_id(public_key_pem)
    entries = _entries(data)
    manifest = _load(_signed(entries, MANIFEST, public_key_pem), MANIFEST)
    if manifest.get("schema") != SCHEMA:
        raise Refused(f"{MANIFEST} is not {SCHEMA}")
    card_bytes = _signed(entries, "proof-card.json", public_key_pem)
    if _sha(card_bytes) != manifest.get("proof_card_sha256"):
        raise Refused("The proof card is not the one the skill was exported with")
    card = _load(card_bytes, "proof-card.json")
    listed = manifest.get("copies")
    if not isinstance(listed, list) or not listed:
        raise Refused(f"{MANIFEST} lists no copies")
    if not all(isinstance(item, dict) for item in listed):
        raise Refused(f"{MANIFEST} lists a copy that is not an object")
    known = {
        MANIFEST,
        f"{MANIFEST}.sig",
        "proof-card.json",
        "proof-card.json.sig",
        "proof-card.pdf",
        "vigil-proof-key.pub",
        "README.txt",
    } | {str(item.get("file")) for item in listed}
    extra = sorted(set(entries) - known)
    if extra:
        raise Refused(
            f"The zip holds files the skill does not list: {', '.join(extra[:5])}"
        )
    if not isinstance(card.get("rules"), list) or not all(
        isinstance(r, dict) for r in card["rules"]
    ):
        raise Refused("proof-card.json lists its rules in a shape it can't be read in")
    proven = {r.get("rule_id"): r for r in card["rules"]}
    copies: List[Dict[str, Any]] = []
    for item in listed:
        language = item.get("language")
        if language not in SAVE_ARG or language not in overlay.LAYOUT:
            raise Refused(f"This Vigil can't hold a {language} copy")
        if any(c["language"] == language for c in copies):
            raise Refused(f"{MANIFEST} lists two {language} copies")
        body = entries.get(str(item.get("file")))
        if body is None or _sha(body) != item.get("sha256"):
            raise Refused(f"{item.get('file')} is missing or not the file signed")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refused(f"{item.get('file')} is not UTF-8 text") from exc
        entry = proven.get(item.get("rule_id"))
        if entry is None:
            raise Refused(f"The proof card has no entry for {item.get('file')}")
        try:
            record = overlay._parse(language, text)
        except overlay.Refused as exc:
            raise Refused(str(exc)) from exc
        if _sha(record.query.encode()) != entry.get("query_sha256"):
            raise Refused(f"{item.get('file')}'s query is not the one the card proves")
        if record.native_key != item.get("native_key"):
            raise Refused(f"{item.get('file')}'s id is not the one signed")
        copies.append(
            {
                "language": language,
                "native_key": record.native_key,
                "text": text,
                "exceptions": item.get("exceptions") or [],
                "proven": entry,
            }
        )
    return {
        "name": manifest.get("name"),
        "manifest": manifest,
        "card": card,
        "copies": copies,
    }


def _held(keys: List[str]) -> List[str]:
    """The keys this Vigil holds: any copy but one a failed import removed, which
    it only keeps the record of (a new import brings it back)."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.native_key,
                DetectionRule.status,
                DetectionRuleVersion.checks,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.native_key.in_(keys),
            )
        ).all()
    return [
        key
        for key, status, checks in rows
        if not (status == "removed" and sync.IMPORT_FAILED in (checks or {}))
    ]


def _save(
    skill: Dict[str, Any],
    username: str,
    proven_event_ids: Optional[dict],
    ruled_out_events: Optional[list],
) -> Dict[str, Any]:
    """Save the skill's copies as one new rule, all or nothing; Refused when held
    or refused by the save."""
    keys = [c["native_key"] for c in skill["copies"]]
    held = _held(keys)
    if held:
        raise Refused(
            f"This Vigil already holds {', '.join(sorted(held))}: an import adds "
            "a new rule and never changes one here"
        )
    kwargs = {SAVE_ARG[c["language"]]: c["text"] for c in skill["copies"]}
    given = {c["language"]: c["exceptions"] for c in skill["copies"] if c["exceptions"]}
    with acting_as(username):
        saved = overlay.save_rule(
            proven_event_ids=proven_event_ids,
            ruled_out_events=ruled_out_events,
            exceptions=given or None,
            all_or_nothing=True,
            **kwargs,
        )
    if saved.get("error"):
        raise Refused(saved["error"])
    return saved


def import_skill(
    data: bytes,
    public_key_pem: str,
    actor: str,
    username: str,
    proven_event_ids: Optional[dict] = None,
    ruled_out_events: Optional[list] = None,
) -> Dict[str, Any]:
    """Save a verified skill's copies as a new rule, in shadow, as ``username``
    (``actor`` is their principal id, for the trail); ``{"error": ...}`` when refused.
    """
    kid = None
    try:
        kid = _key_id(public_key_pem) if (public_key_pem or "").strip() else None
        try:
            skill = read(data, public_key_pem)
        except Refused:
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- every import is refused or saved, and audited
            logger.exception("Detection Skill import: the skill could not be read")
            raise Refused(
                f"The skill could not be read ({type(exc).__name__})"
            ) from exc
        try:
            saved = _save(skill, username, proven_event_ids, ruled_out_events)
        except Refused:
            raise
        except overlay.NotSaved as exc:
            logger.exception("Detection Skill import: the save failed")
            if not exc.undone:
                why = "and undoing it failed: check the overlay for its copies"
            elif exc.removed:
                why = (
                    "its copies were saved and then removed, so none is active; "
                    "import the skill again to retry"
                )
            else:
                why = "nothing was changed"
            raise Refused(f"The import didn't complete ({exc}); {why}") from exc
        except Exception as exc:  # noqa: BLE001 -- a fault before any write
            logger.exception("Detection Skill import: the save failed")
            raise Refused(
                f"The import didn't complete ({type(exc).__name__}); nothing was changed"
            ) from exc
    except Refused as exc:
        trail_events.refused(
            IMPORT_REFUSED, actor=actor, key_id=kid, reason=str(exc)[:500]
        )
        return {"error": str(exc)}
    manifest = skill["manifest"]
    trail_events.safety(
        IMPORTED,
        actor=actor,
        name=skill["name"],
        rule_ids=[s["rule_id"] for s in saved["saved"]],
        languages=[c["language"] for c in skill["copies"]],
        exported_from=manifest.get("exported_from"),
        exported_at=manifest.get("exported_at"),
        key_id=kid,
        sha256=_sha(proof_card.canonical(manifest)),
    )
    return {
        **saved,
        "name": skill["name"],
        "exported_from": manifest.get("exported_from"),
        "exported_at": manifest.get("exported_at"),
        "key_id": kid,
        "proven_elsewhere": [
            {
                "language": c["language"],
                "checks": c["proven"].get("checks") or {},
                "path_to_live": c["proven"].get("path_to_live") or [],
            }
            for c in skill["copies"]
        ],
        "linked_proof": [
            r
            for r in skill["card"].get("rules") or []
            if r.get("rule_id")
            not in {c["proven"].get("rule_id") for c in skill["copies"]}
        ],
    }
