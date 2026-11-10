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

Attack events named by their SIEM ids (``proven_event_ids``) prove the save
before anything is written (#4212): Vigil reads them from the SIEM, replays the
Sigma form on them (``siem_replay``), and runs each Splunk and Elastic copy over
their window the way shadow does; each must hit a named event. Each copy's
result lands as ``checks.replay`` (source siem) on its version, and a Splunk or
Elastic copy leaves shadow only with a passed one (``rollout.unproven``).
Events a caller writes are never proof.

Exceptions passed per copy (#3794) are stored apart from the files once the
sync has the copies; the replay counts a ruled-out event that a Sigma exception
(passed, or already on the Sigma copy) matches as excluded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tomllib
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import select, update

from core.detections import exceptions as excepts
from core.detections import wazuh_xml as wxml
from core.detections.detection_rules_service import DetectionRulesService
from core.detections.engineering import siem_replay
from core.detections.inventory import kibana, readers, rollout, sees
from core.detections.inventory import splunk as splunk_inv
from core.detections.inventory import sync
from core.detections.inventory.record import RuleRecord, fingerprint
from core.detections.lint import lint_record, lint_sigma
from core.detections.replay import replay_sigma
from core.integrations._base import search_budget
from core.integrations.splunk.descriptor import SPLUNK
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
NAMED_SIEMS = ("splunk", "elastic", "wazuh")
MAX_NAMED = 50
# A native copy runs over the named events' own times, widened this little: its
# page holds rollout.MAX_DOCS rows, and a wide window could push them off it.
PROOF_PAD = timedelta(minutes=1)
WAZUH_NOT_RUN = (
    "a Wazuh copy has no on-demand run: a hit in its shadow runs over "
    "wazuh-archives-* is its proof"
)


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
        # Copies of the save are held as removed rules, marked as a failed import's.
        self.removed = removed


def _undo(
    service: Any, root: str, before: List[Tuple[str, Optional[str]]], keys: List[str]
) -> Tuple[bool, bool]:
    """Put the files written back as they were and sync the overlay again, so any
    copy of ``keys`` a sync took in (this save's own, or another run's between the
    write and the fault) is removed, marked as removed by a failed import.

    (True when that worked, True when copies of ``keys`` now sit as removed rules.)
    """
    ok = True
    try:
        for rel, text in reversed(before):
            path = Path(DetectionRulesService._under(root, rel))
            if text is None:
                path.unlink(missing_ok=True)
            else:
                _write(root, rel, text)
        report = sync._sync_one(
            service, service.overlay_source(), failed_import=set(keys)
        )
        if report.error:
            raise RuntimeError(report.error)
    except Exception:  # noqa: BLE001 -- judged below by what the rules say
        logger.exception("Overlay save: undoing a failed save failed")
        ok = False
    statuses = _mark_failed_import(keys)
    # A sync that faulted before it kept anything left no copy to remove: the
    # undo worked unless a copy is still active.
    ok = ok or (not any(st != "removed" for st in statuses))
    return ok, bool(statuses)


def _mark_failed_import(keys: List[str]) -> List[str]:
    """Mark every removed copy of ``keys`` as a failed import's, whoever removed it
    (a sync that ran between the undo's file restore and its own sync removes them
    unmarked). Safe: an import is refused while a copy of its keys is held, so a
    removed one is either marked already or was removed since this import began.
    Returns the statuses of the copies there are."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(DetectionRule.status, DetectionRuleVersion)
            .join(
                DetectionRuleVersion,
                (DetectionRuleVersion.rule_id == DetectionRule.id)
                & (DetectionRuleVersion.version == DetectionRule.latest_version),
            )
            .where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.native_key.in_(keys),
            )
        ).all()
        for status, version in rows:
            if status == "removed" and sync.IMPORT_FAILED not in (version.checks or {}):
                version.checks = {
                    **(version.checks or {}),
                    sync.IMPORT_FAILED: {"at": datetime.now(timezone.utc).isoformat()},
                }
        return [status for status, _ in rows]


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


def _copy_matches(key: Optional[str], given: List[Tuple[Dict, str]]) -> List[Dict]:
    """A copy's active exceptions, stored and given."""
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


def _named(ids: Any) -> Dict[str, List[str]]:
    """``proven_event_ids`` checked: SIEM -> its event ids, each named once."""
    if not ids:
        return {}
    if not isinstance(ids, dict):
        raise Refused("proven_event_ids must map a SIEM to a list of its event ids")
    out: Dict[str, List[str]] = {}
    for siem, values in ids.items():
        if siem not in NAMED_SIEMS:
            raise Refused(
                f"proven_event_ids names {siem!r}: name splunk, elastic or wazuh"
            )
        if not isinstance(values, list) or not all(
            isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()
            for v in values
        ):
            raise Refused(f"proven_event_ids.{siem} must be a list of event ids")
        clean = list(dict.fromkeys(str(v).strip() for v in values))
        if len(clean) > MAX_NAMED:
            raise Refused(f"name at most {MAX_NAMED} {siem} events")
        # _cd alone is unique only within its bucket: it could name another event.
        bare = [v for v in clean if siem == "splunk" and "~" not in v]
        if bare:
            raise Refused(
                f"name each Splunk event as _bkt:_cd, as its row gives them: "
                f"{', '.join(bare[:3])} has no bucket"
            )
        if clean:
            out[siem] = clean
    return out


def _sigma_proof(
    sigma: Optional[str],
    named: Dict[str, List[str]],
    ruled_out: Optional[list],
    excepted: List[Dict],
    elastic_index: Optional[str] = None,
) -> Dict[str, Any]:
    """``checks.replay`` for the Sigma form, replayed on the named events as the
    SIEMs hold them; Refused when it hits none of them or matches a ruled-out event.
    False positives and a cut-short normal read are recorded, never refused."""
    if not sigma:
        raise Refused(
            "proven_event_ids were passed, but there is no sigma form to replay them on"
        )
    if ruled_out is not None and not isinstance(ruled_out, list):
        raise Refused("ruled_out_events must be a list")
    # A ruled-out event a Sigma exception matches is left out, as before.
    left_out = [
        e for e in ruled_out or [] if any(excepts.matches(m, e) for m in excepted)
    ]
    kept_out = [e for e in ruled_out or [] if not any(e is x for x in left_out)]
    if kept_out:
        local = replay_sigma(sigma, [], kept_out)
        if not local["evaluated"]:
            raise Refused(
                f"sigma form could not be replayed: {local['reason']}", replay=local
            )
        if local["ruled_out_matched"]:
            raise Refused("sigma form matched a ruled-out event", replay=local)
    evidence = asyncio.run(
        siem_replay.run(
            sigma, named=named, excepted=excepted, elastic_index=elastic_index
        )
    )
    if evidence.get("siems") is None:  # never compiled: nothing was read
        raise Refused(
            f"sigma form could not be replayed: {evidence['unproven_because']}",
            replay=evidence,
        )
    by_siem = {s["siem"]: s for s in evidence["siems"]}
    for siem in named:
        s = by_siem.get(siem)
        why = (
            "Vigil does not read it ("
            + "; ".join(evidence.get("skipped") or ["not configured"])
            + ")"
            if s is None
            else s.get("deferred") or s.get("error")
        )
        if why:
            raise Refused(
                f"the named {siem.title()} events could not be read: {why}",
                replay=evidence,
            )
    missing = {
        siem: by_siem[siem]["named_missing"]
        for siem in named
        if by_siem[siem].get("named_missing")
    }
    if not evidence.get("hits"):
        raise Refused(
            "sigma form matched none of the named events" + _missing_text(missing),
            replay=evidence,
            missed=True,
        )
    return {
        **evidence,
        "source": "siem",
        "result": "pass",
        "attack_basis": "named in proven_event_ids",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "named": named,
        "missing": missing,
        "ruled_out_events": len(kept_out),
        "ruled_out_excepted": len(left_out),
    }


def _missing_text(missing: Dict[str, List[str]]) -> str:
    if not missing:
        return ""
    parts = [
        f"{siem.title()} has no event {', '.join(ids)}" for siem, ids in missing.items()
    ]
    return f" ({'; '.join(parts)}: a named id the SIEM does not return proves nothing)"


async def _run_native(
    language: str, rule: Dict[str, Any], key: str, start: datetime, end: datetime
) -> Tuple[int, List[Dict[str, Any]]]:
    """Run a native copy over [start, end) the way shadow does, on its SIEM's budget."""
    copy = rollout.Copy(0, 0, key, language, "shadow", rollout.NO_STAGE_AT, {}, rule)
    if language == "splunk":
        config = rollout._splunk_config()
        if config is None:
            raise Refused(
                "the splunk copy cannot be run: splunk is not enabled or missing its login"
            )
        integration, per_hour = SPLUNK.id, splunk_inv.per_hour_of(config)
        service, query = splunk_inv.service_of(config), rollout._splunk_query
    else:
        config = rollout._elastic_config()
        if config is None:
            raise Refused(
                "the elastic copy cannot be run: elastic-siem is not enabled or has no elasticsearch_url"
            )
        cap = config.get("max_searches_per_hour")
        per_hour = sees.DEFAULT_PER_HOUR if cap is None else max(int(cap), 0)
        integration = rollout.INTEGRATION
        service, query = kibana.service_of(config), rollout._query
    try:
        if not search_budget.spend(integration, per_hour, f"overlay-save:{key}"):
            raise Refused(
                f"the {language} copy was not run: {language.title()}'s search budget "
                "for this hour is spent; save again later"
            )
        return await query(service, copy, start, end)
    finally:
        close = getattr(service, "close", None)
        done = close() if close is not None else None
        if asyncio.iscoroutine(done):
            await done


def _native_proof(
    language: str,
    text: str,
    key: str,
    named: Dict[str, List[str]],
    sigma: Dict[str, Any],
    excepted: List[Dict],
) -> Dict[str, Any]:
    """``checks.replay`` for a Splunk or Elastic copy: run in its SIEM over the
    named events' own window, it must return one of them. A copy with no event
    named for its SIEM is recorded not_run, and stays in shadow."""
    ids = named.get(language) or []
    if not ids:
        return {
            "source": "siem",
            "result": "not_run",
            "reason": f"no {language} event was named in proven_event_ids",
        }
    read = next(s for s in sigma["siems"] if s["siem"] == language)
    found = read.get("named_found") or []
    times = [datetime.fromisoformat(f["at"]) for f in found if f.get("at")]
    if not times:
        raise Refused(
            f"the {language} copy cannot be checked: {language.title()} returned none "
            "of the named events" + ("" if not found else " with a time")
        )
    rule = (
        yaml.safe_load(text)
        if language == "splunk"
        else tomllib.loads(text).get("rule")
    ) or {}
    start, end = min(times) - PROOF_PAD, max(times) + PROOF_PAD
    if rule.get("timestamp_override"):
        # The copy's window is on its own field (event.ingested): an event lands
        # there after its @timestamp, by up to the lag shadow allows for.
        end += rollout.LAG
    try:
        total, docs = asyncio.run(_run_native(language, rule, key, start, end))
    except Refused:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 -- the SIEM's own error, named in the refusal
        raise Refused(
            f"the {language} copy failed to run: {str(exc)[:500] or type(exc).__name__}"
        )
    kept, _, _ = excepts.split(docs, list(enumerate(excepted)))
    # An EQL sequence is one row whose id joins its events' ids with "+".
    returned = [part for d in kept for part in rollout._doc_id(d).split("+")]
    hits = [n for n in ids if any(siem_replay.same_id(r, n) for r in returned)]
    window = {"start": start.isoformat(), "end": end.isoformat()}
    if not hits:
        why = f"the {language.title()} copy returned 0 of the {len(found)} named events"
        if len(kept) < len(docs):
            why += f" ({len(docs) - len(kept)} rows dropped by its exceptions)"
        if kept and not any(_has_id(language, d) for d in kept):
            why += (
                f"; its {len(kept)} rows carry no event id, so none can be matched to "
                "a named event: "
                + (
                    "a Splunk search must return events (with _bkt and _cd), not a "
                    "stats table"
                    if language == "splunk"
                    else "an ES|QL query must keep METADATA _id"
                )
            )
        if len(docs) >= rollout.MAX_DOCS:
            why += (
                f"; its page of {rollout.MAX_DOCS} rows was full, so a named event "
                "may have fallen off it: make the search narrower"
            )
        raise Refused(
            why,
            replay={
                "siem": language,
                "window": window,
                "returned": total,
                "named": ids,
            },
        )
    return {
        "source": "siem",
        "result": "pass",
        "siem": language,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "window": window,
        "named": ids,
        "missing": read.get("named_missing") or [],
        "hits": hits,
        "returned": total,
        "samples": [
            rollout._sample(d)
            for d in kept
            if any(siem_replay.same_id(rollout._doc_id(d), n) for n in hits)
        ][: rollout.SAMPLE],
    }


def _has_id(language: str, doc: Dict[str, Any]) -> bool:
    if language == "splunk":
        return bool((doc.get("_source") or {}).get("_cd"))
    return bool(doc.get("_id"))


def _evidence(
    copies: Dict[str, str],
    keys: Dict[str, str],
    proven_event_ids: Any,
    ruled_out: Optional[list],
    wanted: Dict[str, List[Tuple[Dict, str]]],
) -> Dict[str, Dict[str, Any]]:
    """``checks.replay`` per copy language; Refused when the SIEM does not hold the rule up."""
    named = _named(proven_event_ids)
    if not named:
        if ruled_out:
            raise Refused(
                "ruled_out_events were passed without proven_event_ids: name the "
                "attack events by their SIEM ids"
            )
        return {language: {"result": "not_run"} for language in copies}
    refused: List[Refused] = []
    try:
        sigma = _sigma_proof(
            copies.get("sigma"),
            named,
            ruled_out,
            _copy_matches(keys.get("sigma"), wanted.get("sigma", [])),
            _elastic_index(copies.get("elastic")),
        )
    except Refused as exc:
        # Once the named events were read, each native copy is still run, so the
        # refusal names every copy that misses them.
        if not exc.detail.pop("missed", False):
            raise
        sigma = exc.detail["replay"]
        refused.append(exc)
    out: Dict[str, Dict[str, Any]] = {"sigma": sigma}
    for language in ("splunk", "elastic"):
        if language in copies:
            try:
                out[language] = _native_proof(
                    language,
                    copies[language],
                    keys[language],
                    named,
                    sigma,
                    _copy_matches(keys[language], wanted.get(language, [])),
                )
            except Refused as exc:
                refused.append(exc)
    if refused:
        raise Refused(
            "; ".join(str(r) for r in refused),
            replay=refused[0].detail.get("replay"),
            more=[r.detail.get("replay") for r in refused[1:]],
        )
    if "wazuh" in copies:
        out["wazuh"] = {"source": "siem", "result": "not_run", "reason": WAZUH_NOT_RUN}
    return out


def _elastic_index(text: Optional[str]) -> Optional[str]:
    """The Elastic copy's own index with the replay's default ones, so a named
    event in its index is read."""
    if not text:
        return None
    own = rollout._index(tomllib.loads(text).get("rule") or {})
    return f"{siem_replay.ELASTIC_INDICES},{own}" if own else None


def _record_replays(replays: Dict[str, Dict[str, Any]]) -> None:
    """Set ``checks.replay`` on each copy's latest version, by native key. A new
    version starts with no checks, so it carries no earlier proof."""
    with get_db_manager().session_scope() as session:
        for key, replay in replays.items():
            rule = session.execute(
                select(DetectionRule.id, DetectionRule.latest_version).where(
                    DetectionRule.source == readers.OVERLAY_SOURCE,
                    DetectionRule.native_key == key,
                )
            ).first()
            if rule is None:
                continue
            version = (DetectionRuleVersion.rule_id == rule.id) & (
                DetectionRuleVersion.version == rule.latest_version
            )
            checks = session.scalar(select(DetectionRuleVersion.checks).where(version))
            # A re-save without ids leaves an unchanged version's earlier proof standing.
            if replay["result"] == "not_run" and (checks or {}).get("replay"):
                continue
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
    proven_event_ids: Optional[Dict[str, List[str]]] = None,
    ruled_out_events: Optional[list] = None,
    exceptions: Optional[Dict[str, Any]] = None,
    wazuh_xml: Optional[str] = None,
    all_or_nothing: bool = False,
) -> Dict[str, Any]:
    """Write each copy into the overlay and sync it; refuse the lot if any part fails.

    With ``all_or_nothing``, a fault after the first file is written (a write, the
    sync, the replay record or the exceptions) is undone before it is raised as
    ``NotSaved``: the files written are put back as they were and the overlay is
    synced again, so no copy is left active without its exceptions. A copy that
    leaves is marked as removed by a failed import, and the same skill can be
    imported again."""
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
        by_language = {lang: r.native_key for lang, _, r in prepared}
        replays = _evidence(
            {lang: text for lang, text, _ in prepared},
            by_language,
            proven_event_ids,
            ruled_out_events,
            wanted,
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
            if not report.error:
                _record_replays({by_language[lang]: r for lang, r in replays.items()})
            added = [] if report.error else _add_exceptions(prepared, wanted)
        except Exception as exc:
            if not all_or_nothing:
                raise
            undone, removed = _undo(service, root, before, keys)
            reason = exc.args[0] if isinstance(exc, NotSaved) else type(exc).__name__
            raise NotSaved(reason, undone=undone, removed=removed) from exc
    result: Dict[str, Any] = {"saved": _saved(keys), "sync": asdict(report)}
    if added:
        result["exceptions"] = added
    if report.error:
        result["error"] = f"written, but the overlay sync failed: {report.error}"
    return result
