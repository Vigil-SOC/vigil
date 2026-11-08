"""Save a drafted rule into Vigil's overlay source, then sync that source (#3723).

The overlay is a plain directory under the State Directory, read like any
local source in format 'auto': one file per copy, ``splunk/<id>.yml`` (ESCU
YAML), ``elastic/<id>.toml`` (detection-rules TOML), ``sigma/<id>.yml``,
``wazuh/<id>.xml`` (one Wazuh rule, its id in Wazuh's custom range). Every
copy is parsed with the inventory readers, and the Sigma form and each Splunk
and Elastic query linted, before any file is written; one bad part writes nothing. A copy keeps the id it
carries, so a change to a rule lands on that rule as a new version; a copy
without one gets a fresh uuid. The sync appends the 'shadow' stage rows, and for each copy's new version one
``detections.rule_saved`` trail entry naming who saved it (#4097), in the same
transaction. A copy saved unchanged makes no version and no entry. A version
with no such entry was first seen by the sync alone: a file put in the folder.

Evidence passed with the save is replayed on the Sigma form before anything is
written (#3755); the result lands as ``checks.replay`` on the Sigma version.

Exceptions passed per copy (#3794) are stored apart from the files once the
sync has the copies; the replay counts a ruled-out event that a Sigma exception
(passed, or already on the Sigma copy) matches as excluded.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import select, update

from core.detections import exceptions as excepts
from core.detections import wazuh_xml as wxml
from core.detections.detection_rules_service import DetectionRulesService
from core.detections.inventory import readers, rollout, sync
from core.detections.inventory.record import RuleRecord, fingerprint
from core.detections.lint import lint_record, lint_sigma
from core.detections.replay import replay_sigma
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
)

logger = logging.getLogger(__name__)

# Copy language: (overlay subdirectory, file suffix).
LAYOUT = {
    "splunk": ("splunk", ".yml"),
    "elastic": ("elastic", ".toml"),
    "sigma": ("sigma", ".yml"),
    "wazuh": ("wazuh", ".xml"),
}
# The [rule] header line, with any comment or CR after it.
_RULE_TABLE = re.compile(r"^[ \t]*\[[ \t]*rule[ \t]*\][^\n]*$", re.MULTILINE)
# A leading YAML document marker: the id goes under it, not above.
_YAML_START = re.compile(r"\A(?:[ \t]*(?:#[^\n]*)?\n)*---[^\n]*\n")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class Refused(ValueError):
    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = detail


class NotSaved(Exception):
    """An all-or-nothing save that failed after writing; ``undone`` says whether
    the overlay and its rules were put back, ``removed`` that its copies had
    been synced and are now removed rules."""

    def __init__(self, reason: str, undone: bool = True, removed: bool = False):
        super().__init__(reason)
        self.undone = undone
        # The sync had committed: the copies are back out, as removed rules.
        self.removed = removed


def _undo(
    service: Any, root: str, before: List[Tuple[str, Optional[str]]], committed: bool
) -> bool:
    """Put the files written back as they were and, if their sync committed, sync
    the overlay again so the copies it added are removed. True when that worked."""
    try:
        for rel, text in reversed(before):
            path = Path(DetectionRulesService._under(root, rel))
            if text is None:
                path.unlink(missing_ok=True)
            else:
                _write(root, rel, text)
        if committed:
            report = sync._sync_one(service, service.overlay_source())
            if report.error:
                raise RuntimeError(report.error)
        return True
    except Exception:  # noqa: BLE001 -- the caller says the undo failed
        logger.exception("Overlay save: undoing a failed save failed")
        return False


def _with_id(language: str, text: str) -> str:
    """``text`` with a fresh native id when it carries none."""
    fresh = str(uuid.uuid4())
    if language == "elastic":
        try:
            rule = tomllib.loads(text).get("rule")
        except tomllib.TOMLDecodeError as exc:
            raise Refused(f"elastic copy is not valid TOML: {exc}") from exc
        if not isinstance(rule, dict):
            raise Refused("elastic copy has no [rule] table")
        if rule.get("rule_id"):
            return text
        # No TOML writer in the stdlib: the id goes on the line under [rule].
        return _RULE_TABLE.sub(lambda m: f'{m.group(0)}\nrule_id = "{fresh}"', text, 1)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Refused(f"{language} copy is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refused(f"{language} copy must be a YAML mapping")
    if doc.get("id"):
        return text
    if "id" in doc:
        raise Refused(f"{language} copy has an empty id: give one, or leave it out")
    start = _YAML_START.match(text)
    at = start.end() if start else 0
    return f"{text[:at]}id: {fresh}\n{text[at:]}"


def _wazuh_ids(session: Any) -> Tuple[set, set]:
    """(ids the Wazuh manager has loaded, as the inventory read them; ids the
    overlay's own Wazuh copies hold)."""
    rows = session.execute(
        select(DetectionRule.source, DetectionRule.native_key).where(
            DetectionRule.status != "removed",
            (DetectionRule.source == "wazuh")
            | (
                (DetectionRule.source == readers.OVERLAY_SOURCE)
                & (DetectionRule.language == "wazuh")
            ),
        )
    ).all()
    loaded = {key for source, key in rows if source == "wazuh"}
    own = {key for source, key in rows if source == readers.OVERLAY_SOURCE}
    return loaded, own


def _wazuh_with_id(text: str) -> str:
    """``text`` with an id in Wazuh's custom range that no other rule holds.

    A given id must be in the range, and not one the manager has loaded unless
    it is this overlay copy's own (a rule Vigil deployed is loaded under its id).
    With none, the lowest free id is set.
    """
    try:
        rule = wxml.parse(text)
    except wxml.NotARule as exc:
        raise Refused(f"wazuh copy is refused: {exc}") from exc
    first, last = wxml.CUSTOM_IDS[0], wxml.CUSTOM_IDS[-1]
    with get_db_manager().session_scope() as session:
        loaded, own = _wazuh_ids(session)
    # Compared as the numbers the manager reads ids as ("0100001" is 100001).
    loaded_ids = {n for n in map(wxml.number, loaded) if n is not None}
    own_ids = {n for n in map(wxml.number, own) if n is not None}
    given = wxml.rule_id(rule)
    if given is None:
        taken = loaded_ids | own_ids
        free = next((i for i in wxml.CUSTOM_IDS if i not in taken), None)
        if free is None:
            raise Refused(f"no free Wazuh rule id is left in {first}-{last}")
        return wxml.with_id(text, free)
    number = wxml.custom_id(given)
    if number is None:
        raise Refused(
            f"wazuh copy's id {given} is outside Wazuh's range for custom rules "
            f"({first}-{last}, written in ASCII digits with no leading zero): "
            "use one in it, or leave the id out"
        )
    if number in loaded_ids and number not in own_ids:
        raise Refused(
            f"wazuh copy's id {given} is a rule the Wazuh manager already loaded: "
            "use another, or leave the id out"
        )
    return text


def _parse(language: str, text: str) -> RuleRecord:
    """The copy as the overlay's own reader will see it; Refused unless it reads as ``language``."""
    _, suffix = LAYOUT[language]
    try:
        record = readers.parse_file(
            "auto", Path(f"draft{suffix}"), f"draft{suffix}", text
        )
    except Exception as exc:  # noqa: BLE001 -- whatever the reader rejects
        raise Refused(f"{language} copy does not parse: {exc}") from exc
    if record is None or record.language != language:
        found = record.language if record else "no rule"
        raise Refused(f"{language} copy reads as {found}")
    if language != "sigma" and not record.query.strip():
        raise Refused(f"{language} copy has no query")
    if record.native_key == f"draft{suffix}":
        raise Refused(f"{language} copy's id could not be read or set")
    return record


def _wanted(
    given: Optional[Dict[str, Any]], copies: Dict[str, str]
) -> Dict[str, List[Tuple[Dict[str, Any], str]]]:
    """Exceptions per copy language as (match, note); Refused when one is malformed."""
    if not given:
        return {}
    if not isinstance(given, dict):
        raise Refused(
            "exceptions must be an object keyed by copy: splunk, elastic, sigma"
        )
    out = {}
    for language, items in given.items():
        if language not in copies:
            raise Refused(f"exceptions name a {language} copy that is not being saved")
        if language == "wazuh":
            # Exceptions drop hits Vigil fetches; a Wazuh copy's are archive counts only.
            raise Refused("exceptions are not kept for a wazuh copy")
        if not isinstance(items, list):
            raise Refused(f"{language} exceptions must be a list")
        try:
            out[language] = [
                excepts.checked(
                    (i or {}).get("match") if isinstance(i, dict) else None,
                    i.get("note") if isinstance(i, dict) else None,
                )
                for i in items
            ]
        except ValueError as exc:
            raise Refused(f"{language} exception: {exc}") from exc
    return out


def _sigma_matches(key: Optional[str], given: List[Tuple[Dict, str]]) -> List[Dict]:
    """The Sigma copy's active exceptions, stored and given."""
    stored: List[Dict] = []
    if key:
        with get_db_manager().session_scope() as session:
            rid = session.scalar(
                select(DetectionRule.id).where(
                    DetectionRule.source == readers.OVERLAY_SOURCE,
                    DetectionRule.native_key == key,
                )
            )
        if rid is not None:
            stored = [m for _, m in excepts.active([rid]).get(rid, [])]
    return stored + [m for m, _ in given]


def _evidence(
    sigma: Optional[str],
    proven: Optional[list],
    ruled_out: Optional[list],
    excepted: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """``checks.replay`` for the Sigma form; Refused when the evidence does not hold it up."""
    if not proven and not ruled_out:
        return {"result": "not_run"}
    if not sigma:
        raise Refused(
            "events were passed, but there is no sigma form to replay them on"
        )
    excepted = excepted or []

    def hit(e: Any) -> bool:
        return any(excepts.matches(m, e) for m in excepted)

    if any(hit(e) for e in proven or []):
        raise Refused(
            "a proven event matches an exception: the rule would never alert on it"
        )
    left_out = [e for e in ruled_out or [] if hit(e)]
    ruled_out = [e for e in ruled_out or [] if not hit(e)]
    replay = replay_sigma(sigma, proven or [], ruled_out)
    replay["ruled_out_excepted"] = len(left_out)
    if not replay["evaluated"]:
        raise Refused(
            f"sigma form could not be replayed: {replay['reason']}", replay=replay
        )
    if replay["ruled_out_matched"]:
        raise Refused("sigma form matched a ruled-out event", replay=replay)
    if not replay["matched"]:
        raise Refused("sigma form matched no proven event", replay=replay)
    return {
        "result": "pass",
        **replay,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "proven_events": len(proven or []),
        "ruled_out_events": len(ruled_out or []),
    }


def _record_replay(sigma_key: str, replay: Dict[str, Any]) -> None:
    """Merge ``replay`` into the checks of the Sigma copy's latest version."""
    with get_db_manager().session_scope() as session:
        rule = session.execute(
            select(DetectionRule.id, DetectionRule.latest_version).where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.native_key == sigma_key,
            )
        ).first()
        if rule is None:
            return
        version = (DetectionRuleVersion.rule_id == rule.id) & (
            DetectionRuleVersion.version == rule.latest_version
        )
        checks = session.scalar(select(DetectionRuleVersion.checks).where(version))
        # A re-save without events leaves an unchanged version's earlier replay standing.
        if replay["result"] == "not_run" and (checks or {}).get("replay"):
            return
        session.execute(
            update(DetectionRuleVersion)
            .where(version)
            .values(checks={**(checks or {}), "replay": replay})
        )


def _prepare(copies: Dict[str, str]) -> List[Tuple[str, str, RuleRecord]]:
    if not ({"splunk", "elastic", "wazuh"} & copies.keys()):
        raise Refused("a rule needs a splunk, an elastic or a wazuh copy")
    prepared = []
    for language, text in copies.items():
        text = _wazuh_with_id(text) if language == "wazuh" else _with_id(language, text)
        if language == "elastic":
            # Vigil runs the copy itself (rollout): only languages it can run.
            why = rollout.runnable(tomllib.loads(text).get("rule") or {})
            if why:
                raise Refused(f"elastic copy is refused: {why}")
        record = _parse(language, text)
        if language in sync.STAGED_LANGUAGES or language == "wazuh":
            lint = lint_record(record)
            if lint and not lint["passed"]:
                raise Refused(
                    f"{language} copy fails lint: {lint.get('rewrite_guidance')}",
                    lint=lint,
                )
        prepared.append((language, text, record))
    if "sigma" in copies:
        lint = lint_sigma(rule_yaml=copies["sigma"])
        if not lint.get("passed"):
            raise Refused(
                f"sigma form fails lint: {lint.get('rewrite_guidance') or lint.get('error')}",
                lint=lint,
            )
    keys = [record.native_key for _, _, record in prepared]
    if len(set(keys)) != len(keys):
        raise Refused("each copy needs its own id")
    return prepared


def _targets(
    root: str, prepared: List[Tuple[str, str, RuleRecord]]
) -> List[Tuple[str, str]]:
    """(file, text) per copy: where the overlay last held it, else a new file.

    Refused when an id already names another kind of copy, or a new file would
    land on one already there: either would turn an existing rule off.
    """
    keys = [record.native_key for _, _, record in prepared]
    with get_db_manager().session_scope() as session:
        stored = {
            key: (language, path)
            for key, language, path in session.execute(
                select(
                    DetectionRule.native_key, DetectionRule.language, DetectionRule.path
                ).where(
                    DetectionRule.source == readers.OVERLAY_SOURCE,
                    DetectionRule.native_key.in_(keys),
                )
            ).all()
        }
    targets = []
    for language, text, record in prepared:
        held, path = stored.get(record.native_key, (language, None))
        if held != language:
            raise Refused(f"id {record.native_key} already names a {held} copy")
        if not path:
            folder, suffix = LAYOUT[language]
            name = _UNSAFE.sub("_", record.native_key).lstrip(".") or "rule"
            path = f"{folder}/{name}{suffix}"
            if Path(root, path).exists():
                raise Refused(f"{path} already holds another rule")
        targets.append((path, text))
    return targets


def _write(root: str, rel: str, text: str) -> None:
    target = Path(DetectionRulesService._under(root, rel))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _saved(keys: List[str]) -> List[Dict[str, Any]]:
    with get_db_manager().session_scope() as session:
        rules = session.scalars(
            select(DetectionRule).where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.native_key.in_(keys),
            )
        ).all()
        stages = dict(
            session.execute(
                select(DetectionRuleStage.rule_id, DetectionRuleStage.stage)
                .distinct(DetectionRuleStage.rule_id)
                .where(DetectionRuleStage.rule_id.in_([r.id for r in rules]))
                .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id.desc())
            ).all()
        )
        by_key = {
            rule.native_key: {
                "rule_id": rule.id,
                "id": rule.native_key,
                "source": rule.language,
                "title": rule.name,
                "file_path": rule.path,
                "status": rule.status,
                "version": rule.latest_version,
                "stage": stages.get(rule.id),
            }
            for rule in rules
        }
    return [by_key[key] for key in keys if key in by_key]


def _add_exceptions(
    prepared: List[Tuple[str, str, RuleRecord]],
    wanted: Dict[str, List[Tuple[Dict[str, Any], str]]],
) -> List[Dict[str, Any]]:
    """Store the exceptions given for each saved copy, attributed to the caller."""
    if not wanted:
        return []
    who = excepts._who()
    added = []
    with get_db_manager().session_scope() as session:
        for language, _, record in prepared:
            if not wanted.get(language):
                continue
            rid = session.scalar(
                select(DetectionRule.id).where(
                    DetectionRule.source == readers.OVERLAY_SOURCE,
                    DetectionRule.native_key == record.native_key,
                )
            )
            if rid is not None:
                added += excepts.add_many(session, rid, wanted[language], who)
    return added


def _author() -> Dict[str, Any]:
    """Who is saving, from the call's signed claims, never an argument the model gave."""
    from core.cases.agent_closure import CALLER_UNAUTHENTICATED, actor, run_agent

    who = actor()
    agent_id, run_id = run_agent()
    if agent_id or run_id:
        by = "agent"  # a run's tool call, headless or in a person's chat
    elif who != CALLER_UNAUTHENTICATED:
        by = "person"  # a signed-in caller outside any run
    else:
        by = "unknown"
    return {
        "actor": sync.trail_actor(who),
        "by": by,
        "agent_id": agent_id,
        "run_id": run_id,
    }


def save_rule(
    splunk_yaml: Optional[str] = None,
    elastic_toml: Optional[str] = None,
    sigma_yaml: Optional[str] = None,
    proven_events: Optional[list] = None,
    ruled_out_events: Optional[list] = None,
    exceptions: Optional[Dict[str, Any]] = None,
    wazuh_xml: Optional[str] = None,
    all_or_nothing: bool = False,
) -> Dict[str, Any]:
    """Write each copy into the overlay and sync it; refuse the lot if any part fails.

    With ``all_or_nothing``, a fault after the first file is written (a write, the
    sync, the replay record or the exceptions) is undone before it is raised as
    ``NotSaved``: the files written are put back as they were and, if the sync had
    committed, the overlay is synced again, so no copy is left active without its
    exceptions."""
    copies = {
        language: text
        for language, text in (
            ("splunk", splunk_yaml),
            ("elastic", elastic_toml),
            ("sigma", sigma_yaml),
            ("wazuh", wazuh_xml),
        )
        if text and text.strip()
    }
    try:
        prepared = _prepare(copies)
        wanted = _wanted(exceptions, copies)
        sigma_key = next(
            (r.native_key for lang, _, r in prepared if lang == "sigma"), None
        )
        replay = _evidence(
            copies.get("sigma"),
            proven_events,
            ruled_out_events,
            _sigma_matches(sigma_key, wanted.get("sigma", [])),
        )
    except Refused as exc:
        return {"error": str(exc), "saved": [], **exc.detail}
    keys = [record.native_key for _, _, record in prepared]
    # Under the sync lock: a running sync must not read a half-saved overlay.
    with sync._sync_lock():
        service = DetectionRulesService()
        source = service.overlay_source()
        try:
            targets = _targets(source["local_path"], prepared)
        except Refused as exc:
            return {"error": str(exc), "saved": []}
        root = source["local_path"]
        before: List[Tuple[str, Optional[str]]] = []
        committed = False
        try:
            for rel, text in targets:
                path = Path(DetectionRulesService._under(root, rel))
                before.append(
                    (rel, path.read_text(encoding="utf-8") if path.exists() else None)
                )
                _write(root, rel, text)
            source = service.overlay_source()  # recounted with the new files
            saves = sync.Saves(
                _author(),
                {record.native_key: fingerprint(record) for _, _, record in prepared},
            )
            report = sync._sync_one(service, source, saves)
            if report.error and all_or_nothing:
                # The sync is one transaction: nothing of it was kept.
                raise NotSaved(f"the overlay sync failed: {report.error}")
            committed = not report.error
            if sigma_key and not report.error:
                _record_replay(sigma_key, replay)
            added = [] if report.error else _add_exceptions(prepared, wanted)
        except Exception as exc:
            if not all_or_nothing:
                raise
            undone = _undo(service, root, before, committed)
            reason = exc.args[0] if isinstance(exc, NotSaved) else type(exc).__name__
            raise NotSaved(reason, undone=undone, removed=committed) from exc
    result: Dict[str, Any] = {"saved": _saved(keys), "sync": asdict(report)}
    if added:
        result["exceptions"] = added
    if report.error:
        result["error"] = f"written, but the overlay sync failed: {report.error}"
    return result
