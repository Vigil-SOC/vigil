"""Bring the detection rule tables in line with every configured repo, Kibana, Splunk and Wazuh.

Each run reads every ready source in full, since a full list is the only way
to see a deletion, and writes only what differs:

- a key never seen before is a new rule at version 1;
- a key whose fingerprint differs from its latest version gets the next version;
- a key the list no longer has is status 'removed', never deleted;
- a name, path, technique or tactic change updates the rule row, no new version;
- a SIEM rule switched off is status 'disabled', and carries no Runs, Sees,
  Quiet, Accurate, Noisy or Silent result (its drift result stays).

A rule that stops carrying a check (disabled, no longer listed, its integration
or source gone, or an overlay copy Kibana now runs) gets a NULL row for each
check whose last recorded result isn't NULL (``_dropped``), which closes any
open flag on it.

A Runs result (checks['runs'] on the latest version) is written only when its
result or reason differs from the stored one; a new version starts with none.
After Wazuh syncs, ``quiet`` counts each active Wazuh rule's alerts against a
per-day threshold and the rule's own 30 days (checks['quiet'], checks['noisy'],
checks['silent'], from ``wazuh-alerts-*`` on the elastic-siem indexer), then ``wazuh_sees`` checks whether each one's decoder gets events;
after Kibana syncs, ``quiet`` counts each active Kibana rule's alerts the same
way and against the rule's own 30 days (checks['noisy'], checks['silent']),
then ``sees`` checks whether each active Kibana rule's data is arriving; after
Splunk syncs, ``quiet`` counts each active saved search's alerts the same way,
Noisy and Silent included, then ``splunk_sees`` checks each active saved search's data the same
way. All three Sees checks write checks['sees']; each check is written the
same way. Last, ``accurate`` judges each active Kibana, Wazuh and Splunk rule's
latest version from the stored findings' verdicts and writes checks['accurate']
the same way (local SQL only). Then, at most once a day, ``caught`` credits each
rule version cited on a matching compose-run step or proven Verdict
(checks['caught'], pass only, never cleared; a disabled rule keeps it).

After the sources, each SIEM rule is linked to the repo rule it came from when
that is certain: a Kibana rule by ``same_id`` (one active repo Elastic rule with
its rule_id), a Splunk saved search by ``known_name`` ('ESCU - <name> - Rule' and
one active repo Splunk rule named <name>). A link or decline an agent or person
made is never touched.

Every change of a check result is also appended to detection_rule_check_changes
(``_record_changes``): a Runs result that differs from the rule's last recorded
one, across versions, and a SIEM rule's status moving between active, disabled
and removed. Repo rules get no rows here (their lint is no check that runs);
Vigil's Elastic overlay copies get their Runs from ``rollout``, which records
them the same way.

After Kibana and Splunk link, ``_drift`` checks each linked copy against its
repo rule and writes checks['drift'] the same way: fail when the copy is disabled
or removed while its repo rule is active, or when only the copy moved (a same_id
copy whose fingerprint differs and whose latest version is newer than both the
link and the repo rule's; a known_name or agent copy that moved after the link
with no repo version since its previous one). A copy that had a drift result and
is no longer linked gets a NULL row, reason 'not linked'. A disabled rule keeps
its drift; only Runs, Sees, Quiet, Accurate, Noisy and Silent are dropped.

A source that fails to read, or is configured but not ready, is skipped whole.
A source no longer configured has its rules marked removed.

Vigil's overlay source alone has rollout stages, for its splunk and elastic
copies (nothing runs a Sigma copy): a new rule or version, or a removed one
back unchanged, appends 'shadow'; a removed one appends 'off'. Each such copy's
query is linted (``lint.lint_record``) into checks['lint'] on every sync, kept
when the verdict is unchanged: a new version that fails gets 'off' instead of
'shadow', and a shadow or live one whose verdict turns to fail goes 'off', the
findings in the proof.

Every repo and local rule (any ``repo:`` source) is linted the same way on a
new version, and once on a version stored before lint ran: checks['lint'], a
Sigma rule's detection and logsource, a Splunk, EQL, ES|QL or Lucene query
whole. One with nothing to lint (a machine learning rule, a KQL query) records
result 'none' and why, once a version too. It never blocks, stages or disables the rule; the catalog stops counting
a failing one as coverage.

Nothing is ever written to a SIEM.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from sqlalchemy import and_, bindparam, func, insert, or_, select, tuple_, update

from core.detections.inventory import (
    accurate,
    caught,
    kibana,
    quiet,
    readers,
    sees,
    splunk,
    splunk_sees,
    wazuh,
    wazuh_sees,
)
from core.detections.inventory.record import RuleRecord, fingerprint
from core.detections.lint import lint_record
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleCheckChange,
    DetectionRuleStage,
    DetectionRuleVersion,
)

logger = logging.getLogger(__name__)

# Serialises whole syncs across the daemon and API workers.
SYNC_LOCK_KEY = 0x7669_6769_6C52_554C  # "vigilRUL"
REPO_PREFIX = "repo:"
# How ESCU names the saved search it ships for a detection.
ESCU_NAME = re.compile(r"^ESCU - (.+) - Rule$")
STAGED_LANGUAGES = ("splunk", "elastic")
# Overlay copies linted on each sync: the staged ones, and Wazuh copies, which a
# person may deploy before Vigil can run them in shadow.
LINTED_LANGUAGES = STAGED_LANGUAGES + ("wazuh",)


@dataclass
class SourceReport:
    source: str
    read: int = 0
    new: int = 0
    versioned: int = 0
    removed: int = 0
    restored: int = 0
    updated: int = 0
    checked: int = 0
    linked: int = 0
    unlinked: int = 0
    drifted: int = 0
    error: Optional[str] = None
    skipped: Optional[str] = None


@dataclass(frozen=True)
class Saves:
    """What ``save_rule`` just wrote into the overlay, for the sync reading it (#4097).

    ``author`` is who saved (see ``saved_entry``); ``fingerprints`` is each
    saved copy's native key and the fingerprint of the content written. A
    version the sync creates whose fingerprint matches gets one SAVED trail
    entry in the sync's own transaction. A version created with none was
    first seen by the sync alone: a file put in the folder by hand.
    """

    author: Dict[str, Any]
    fingerprints: Dict[str, str]


@contextmanager
def _sync_lock() -> Iterator[None]:
    """Session advisory lock held for a whole run, sources read inside it.

    A run that read its sources before another finished would write back what
    that one just changed: a removed source's rules active again, an old version.
    """
    engine = get_db_manager().engine
    if engine is None:
        raise RuntimeError("Database not initialized")
    with engine.connect() as conn:
        conn.execute(select(func.pg_advisory_lock(SYNC_LOCK_KEY)))
        conn.commit()
        try:
            yield
        finally:
            conn.execute(select(func.pg_advisory_unlock(SYNC_LOCK_KEY)))
            conn.commit()


def _version_row(rule_id: int, version: int, record: RuleRecord, fp: str) -> dict:
    return {
        "rule_id": rule_id,
        "version": version,
        "fingerprint": fp,
        "content": record.content,
        "query": record.query,
    }


def _stage_row(
    rule_id: int, version: int, stage: str, reason: str, proof: Optional[dict] = None
) -> dict:
    return {
        "rule_id": rule_id,
        "version": version,
        "stage": stage,
        "actor": "system",
        "reason": reason,
        "proof": proof or {},
    }


def _insert_stages(
    session: Any, rows: List[dict], before: Dict[int, Optional[str]]
) -> None:
    """Insert stage rows, and one trail entry each in the same transaction.

    ``before`` is each rule's stage until now; a rule absent from it is new.
    """
    from core.audit import trail

    session.execute(insert(DetectionRuleStage), rows)
    if not trail.in_use(session):
        return
    for row in rows:
        proof = row.get("proof")
        hold = proof.get("hold") if isinstance(proof, dict) else None
        # A system write: a restore's pause must not roll back the stage rows.
        actor = trail_actor(row["actor"])
        # A person's move also names them as stage rows do, like a Revert's entry.
        person = {"username": row["actor"]} if actor != row["actor"] else {}
        trail.append(
            session,
            staged_entry(
                actor,
                row["rule_id"],
                row["version"],
                before.get(row["rule_id"]),
                row["stage"],
                row["reason"],
                hold,
                **person,
            ),
            gated=False,
        )


STAGED = "detections.rule_staged"
SYSTEM_ACTOR = "system"


def trail_actor(name: str) -> str:
    """Who a trail entry names: a person by their principal id, as every other
    trail entry and the audit screen's actor filter do, and Vigil as 'system'.
    Stage and approval rows keep the username; only the trail entry maps it."""
    if not name or name == SYSTEM_ACTOR:
        return name
    from core.agents.attribution import principal_id_for_username

    return principal_id_for_username(name) or name


def staged_entry(
    actor: str,
    rule_id: int,
    version: int,
    from_stage: Optional[str],
    to_stage: str,
    reason: str,
    hold: Optional[bool],
    **more: Any,
) -> Dict[str, Any]:
    """The one trail entry every stage move writes, Vigil's or a person's.

    ``hold`` is what the move did to a person's hold: True a Revert set it,
    False a release lifted it, None it left it as it was (every move Vigil makes).
    """
    from core.audit import trail

    return {
        trail.ACTION: STAGED,
        trail.ACTOR: actor,
        trail.OBJECT: str(rule_id),
        "rule_id": rule_id,
        "version": version,
        "from_stage": from_stage,
        "to_stage": to_stage,
        "reason": reason,
        "hold": hold,
        **more,
    }


SAVED = "detections.rule_saved"
SAVE_AUDIT_TYPE = "detection_rule_save"


def saved_entry(
    author: Dict[str, Any],
    rule_id: int,
    version: int,
    record: RuleRecord,
    fp: str,
) -> Dict[str, Any]:
    """The one trail entry a rule saved through Vigil leaves, per copy and version.

    ``author`` is ``{actor, by, agent_id?, run_id?}``: ``actor`` the principal id
    the trail names (the person; for a run no person started, its agent) and
    ``by`` "agent" for a run's tool call, "person" for a signed-in caller
    outside one, "unknown" for neither. ``agent_id`` and ``run_id`` are the
    run's signed claims, never arguments the model gave.
    """
    from core.audit import trail

    return {
        trail.ACTION: SAVED,
        trail.ACTOR: author["actor"],
        trail.OBJECT: str(rule_id),
        "by": author["by"],
        **{k: author[k] for k in ("agent_id", "run_id") if author.get(k)},
        "rule_id": rule_id,
        "native_key": record.native_key,
        "version": version,
        "language": record.language,
        "fingerprint": fp,
    }


def _record_saves(session: Any, author: Dict[str, Any], saved: List[tuple]) -> None:
    """One entry per saved copy's new version, in the transaction that wrote the version.

    Not gated, like the stage rows beside it: a restore's pause must not roll
    back the versions. A refused append fails the sync and writes none of them.
    """
    from core.audit import trail
    from core.storage.config_service import ConfigService

    use_trail = trail.in_use(session)
    for rule_id, version, record, fp in saved:
        entry = saved_entry(author, rule_id, version, record, fp)
        if use_trail:
            trail.append(session, entry, gated=False)
            continue
        value = {k: v for k, v in entry.items() if k not in (trail.ACTION, trail.ACTOR)}
        ConfigService(user_id=author["actor"])._create_audit_log(
            session,
            config_type=SAVE_AUDIT_TYPE,
            config_key=f"detection_rule:{rule_id}",
            action="save",
            old_value=None,
            new_value=value,
        )


def _lint(record: RuleRecord, now: str) -> Dict[str, Any]:
    """checks['lint'] for a repo or overlay rule, linted as a save lints it. One
    with nothing to lint gets result 'none' and why, so it is linted once a
    version like the rest; 'none' is no fail, so the rule stays coverage."""
    report = lint_record(record)
    if report is None:
        return {"result": "none", "reason": _no_lint(record), "checked_at": now}
    return {
        "result": "pass" if report["passed"] else "fail",
        "findings": report["findings"],
        # One host left out by name: the rule still passes; move it to an exception.
        "warnings": report.get("warnings") or [],
        "checked_at": now,
    }


def _no_lint(record: RuleRecord) -> str:
    """Why lint_record has nothing to lint in this rule."""
    kind = (record.logic or {}).get("type")
    if kind == "machine_learning":
        return "machine learning rule: reads an ML job, no query"
    language = (record.logic or {}).get("language") or record.language
    return f"no lint for {language} queries"


def _lint_off(lint: Dict[str, Any]) -> tuple:
    """(reason, proof) of the 'off' a copy failing lint gets."""
    literals = ", ".join(f"{f['kind']} {f['value']}" for f in lint["findings"])
    return f"fails lint: keyed to {literals}", {"lint": lint}


def _rule_fields(record: RuleRecord) -> Dict[str, Any]:
    return {
        "name": record.name,
        "language": record.language,
        "techniques": record.techniques,
        "tactics": record.tactics,
        "path": record.path,
    }


DROPPED_WHEN_DISABLED = ("runs", "sees", "quiet", "accurate", "noisy", "silent")


def _checks(stored: Dict[str, Any], record: RuleRecord, now: str) -> Dict[str, Any]:
    """``stored`` with the record's Runs result, or itself when nothing changed."""
    if record.status == "disabled":
        return {k: v for k, v in stored.items() if k not in DROPPED_WHEN_DISABLED}
    runs, old = record.runs, stored.get("runs") or {}
    if runs is None or (
        old.get("result") == runs["result"] and old.get("reason") == runs["reason"]
    ):
        return stored
    return {**stored, "runs": {**runs, "checked_at": now}}


def _change(
    rule_id: int,
    version: int,
    key: str,
    old: Optional[str],
    new: Optional[str],
    reason: Optional[str] = None,
) -> dict:
    return {
        "rule_id": rule_id,
        "version": version,
        "check_key": key,
        "old_result": old,
        "new_result": new,
        "reason": reason,
    }


def _last_results(session: Any, source: str) -> Dict[tuple, tuple]:
    """(rule id, check key) -> (result, reason) last recorded, for ``source``'s rules."""
    c = DetectionRuleCheckChange
    rows = session.execute(
        select(c.rule_id, c.check_key, c.new_result, c.reason)
        .join(DetectionRule, DetectionRule.id == c.rule_id)
        .where(DetectionRule.source == source)
        .distinct(c.rule_id, c.check_key)
        .order_by(c.rule_id, c.check_key, c.changed_at.desc(), c.id.desc())
    ).all()
    return {(r, k): (new, reason) for r, k, new, reason in rows}


def _runs_change(
    rule_id: int, version: int, record: RuleRecord, last: Dict[tuple, tuple]
) -> Optional[dict]:
    """A Runs row when the record's result or reason differs from the last recorded."""
    if record.status == "disabled" or record.runs is None:
        return None  # a disabled rule's change is its status row
    new = (record.runs["result"], record.runs["reason"])
    old = last.get((rule_id, "runs"))
    if old == new:
        return None
    return _change(rule_id, version, "runs", old[0] if old else None, *new)


def _dropped(
    rule_id: int,
    version: int,
    checks: Optional[Dict[str, Any]],
    keys: tuple,
    last: Dict[tuple, tuple],
    reason: str,
) -> tuple[List[dict], Dict[str, Any]]:
    """A rule stops carrying ``keys``: (NULL rows, ``checks`` without them).

    A row is written for each key whose last recorded result isn't NULL, so an
    open flag on it closes.
    """
    rows = [
        _change(rule_id, version, k, last[(rule_id, k)][0], None, reason)
        for k in keys
        if last.get((rule_id, k), (None,))[0] is not None
    ]
    return rows, {k: v for k, v in (checks or {}).items() if k not in keys}


def _write_checks(session: Any, checked: List[tuple]) -> None:
    """Write each (rule id, version, checks); the one column of a version ever updated."""
    if checked:
        session.connection().execute(
            update(DetectionRuleVersion)
            .where(
                DetectionRuleVersion.rule_id == bindparam("rid"),
                DetectionRuleVersion.version == bindparam("ver"),
            )
            .values(checks=bindparam("new_checks")),
            [{"rid": i, "ver": v, "new_checks": c} for i, v, c in checked],
        )


def _drop_all(
    session: Any, rules: List[Any], keys: tuple, reason: str, now: str
) -> None:
    """``_dropped`` for each (id, latest version, source, ...) of ``rules``, written.

    Only rules with a key still recorded non-NULL are touched, so passing ones
    already dropped is free.
    """
    last: Dict[tuple, tuple] = {}
    for source in {r[2] for r in rules}:
        last.update(_last_results(session, source))
    rules = [r for r in rules if any(last.get((r[0], k), (None,))[0] for k in keys)]
    if not rules:
        return
    v = DetectionRuleVersion
    stored = dict(
        session.execute(
            select(v.rule_id, v.checks).where(
                tuple_(v.rule_id, v.version).in_([(r[0], r[1]) for r in rules])
            )
        ).all()
    )
    changes: List[dict] = []
    checked: List[tuple] = []
    for rule_id, version, *_ in rules:
        before = stored.get(rule_id) or {}
        rows, checks = _dropped(rule_id, version, before, keys, last, reason)
        changes += rows
        if checks != before:
            checked.append((rule_id, version, checks))
    _write_checks(session, checked)
    _record_changes(session, changes, now)


def _current_stages(session: Any) -> Dict[int, str]:
    """rule id -> current stage, for the overlay's staged copies."""
    s = DetectionRuleStage
    return dict(
        session.execute(
            select(s.rule_id, s.stage)
            .join(DetectionRule, DetectionRule.id == s.rule_id)
            .where(DetectionRule.source == readers.OVERLAY_SOURCE)
            .distinct(s.rule_id)
            .order_by(s.rule_id, s.at.desc(), s.id.desc())
        ).all()
    )


def _record_changes(session: Any, rows: List[dict], now: str) -> None:
    """Append check result changes, in the caller's transaction. Every writer uses it."""
    if rows:
        session.execute(
            insert(DetectionRuleCheckChange),
            [{**r, "changed_at": datetime.fromisoformat(now)} for r in rows],
        )


def sync_source(
    source: str, records: List[RuleRecord], saves: Optional[Saves] = None
) -> SourceReport:
    """Write the differences between ``records`` (all of ``source``) and the tables.

    ``saves`` is only ever given for the overlay, by the save that wrote it.
    """
    report = SourceReport(source=source, read=len(records))
    now = datetime.now(timezone.utc).isoformat()
    siem = not source.startswith(REPO_PREFIX)
    stages: List[dict] = []

    def stage(
        rule_id: int,
        version: int,
        language: str,
        to: str,
        reason: str,
        lint: Optional[Dict[str, Any]] = None,
    ):
        if source == readers.OVERLAY_SOURCE and language in STAGED_LANGUAGES:
            if lint and lint["result"] == "fail":
                # Failing lint: 'off' in place of 'shadow', or out of shadow/live.
                stages.append(_stage_row(rule_id, version, "off", *_lint_off(lint)))
            else:
                stages.append(_stage_row(rule_id, version, to, reason))

    with get_db_manager().session_scope() as session:
        latest = (
            select(
                DetectionRule,
                DetectionRuleVersion.fingerprint,
                DetectionRuleVersion.checks,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(DetectionRule.source == source)
        )
        existing = {
            rule.native_key: (rule, fp, checks)
            for rule, fp, checks in session.execute(latest).all()
        }
        overlay = source == readers.OVERLAY_SOURCE
        last = _last_results(session, source) if siem or overlay else {}
        current = _current_stages(session) if overlay else {}
        changes: List[dict] = []

        new: List[tuple] = []
        versions: List[dict] = []
        # (rule id, version, record, fingerprint) of each version a save made.
        saved: List[tuple] = []
        saved_keys = saves.fingerprints if saves and overlay else {}
        # (rule id, version, checks) to write once the versions exist.
        checked: List[tuple] = []
        for record in records:
            fp = fingerprint(record)
            hit = existing.get(record.native_key)
            if hit is None:
                new.append((record, fp))
                continue
            rule, latest_fp, stored = hit
            staged = overlay and record.language in LINTED_LANGUAGES
            kept = (stored or {}).get("lint") or {}
            if staged:
                # Every staged copy (and an overlay Wazuh copy, deployable by a
                # person) is linted on each sync, so one passed by an older lint
                # is re-judged; an unchanged verdict on the same version is kept.
                lint = _lint(record, now)
                if fp == latest_fp and (
                    kept.get("result"),
                    kept.get("findings"),
                    kept.get("warnings"),
                ) == (lint["result"], lint.get("findings"), lint.get("warnings")):
                    lint = None
            elif not siem and (fp != latest_fp or "lint" not in (stored or {})):
                # Any other imported rule: a new version is linted, and one
                # stored before lint ran, once.
                lint = _lint(record, now)
            else:
                lint = None
            if fp != latest_fp:
                rule.latest_version += 1
                versions.append(_version_row(rule.id, rule.latest_version, record, fp))
                if saved_keys.get(record.native_key) == fp:
                    saved.append((rule.id, rule.latest_version, record, fp))
                report.versioned += 1
                stored = {}
                stage(
                    rule.id,
                    rule.latest_version,
                    record.language,
                    "shadow",
                    "new version",
                    lint,
                )
            elif lint and lint["result"] == "fail" and rule.status != "removed":
                if current.get(rule.id) in ("shadow", "live"):
                    stage(
                        rule.id, rule.latest_version, record.language, "off", "", lint
                    )
            changed = {
                k: v for k, v in _rule_fields(record).items() if getattr(rule, k) != v
            }
            if rule.status != record.status:
                if siem:
                    changes.append(
                        _change(
                            rule.id,
                            rule.latest_version,
                            "status",
                            rule.status,
                            record.status,
                        )
                    )
                if rule.status == "removed":
                    report.restored += 1
                    if fp == latest_fp:
                        # Back unchanged: an active copy never sits at 'off'.
                        stage(
                            rule.id,
                            rule.latest_version,
                            record.language,
                            "shadow",
                            "restored",
                            lint or (stored or {}).get("lint"),
                        )
                else:
                    changed["status"] = record.status
                rule.status = record.status
            for key, value in changed.items():
                setattr(rule, key, value)
            if changed and fp == latest_fp:
                report.updated += 1
            checks = _checks(stored, record, now)
            if lint:
                checks = {**checks, "lint": lint}
            if siem and record.status == "disabled":
                dropped, checks = _dropped(
                    rule.id,
                    rule.latest_version,
                    checks,
                    DROPPED_WHEN_DISABLED,
                    last,
                    f"disabled in {SIEM_NAMES.get(source, source)}",
                )
                changes += dropped
            if checks != stored:
                checked.append((rule.id, rule.latest_version, checks))
            if siem:
                runs = _runs_change(rule.id, rule.latest_version, record, last)
                if runs:
                    changes.append(runs)

        listed = {r.native_key for r in records}
        for key, (rule, _, stored) in existing.items():
            if key in listed:
                continue
            # Already removed ones too, so a check left from before is dropped;
            # drift is _drift's to decide, never dropped here.
            dropped, checks = _dropped(
                rule.id,
                rule.latest_version,
                stored,
                DROPPED_WHEN_DISABLED,
                last,
                f"no longer listed by {source}",
            )
            changes += dropped
            if checks != (stored or {}):
                checked.append((rule.id, rule.latest_version, checks))
            if rule.status != "removed":
                if siem:
                    changes.append(
                        _change(
                            rule.id,
                            rule.latest_version,
                            "status",
                            rule.status,
                            "removed",
                            f"no longer listed by {source}",
                        )
                    )
                rule.status = "removed"
                report.removed += 1
                stage(rule.id, rule.latest_version, rule.language, "off", "removed")

        if new:
            ids = session.scalars(
                insert(DetectionRule).returning(
                    DetectionRule.id, sort_by_parameter_order=True
                ),
                [
                    {
                        "source": source,
                        "native_key": r.native_key,
                        "status": r.status,
                        "latest_version": 1,
                        **_rule_fields(r),
                    }
                    for r, _ in new
                ],
            ).all()
            for rule_id, (r, fp) in zip(ids, new):
                versions.append(_version_row(rule_id, 1, r, fp))
                if saved_keys.get(r.native_key) == fp:
                    saved.append((rule_id, 1, r, fp))
                lint = None if siem else _lint(r, now)
                stage(rule_id, 1, r.language, "shadow", "new rule", lint)
                checks = _checks({}, r, now)
                if lint:
                    checks = {**checks, "lint": lint}
                if checks:
                    checked.append((rule_id, 1, checks))
                if siem:
                    # First sight: none -> its status and Runs result, never a flag.
                    changes.append(_change(rule_id, 1, "status", None, r.status))
                    runs = _runs_change(rule_id, 1, r, {})
                    if runs:
                        changes.append(runs)
            report.new = len(new)
        if versions:
            session.execute(insert(DetectionRuleVersion), versions)
        _write_checks(session, checked)
        report.checked = len(checked)
        _record_changes(session, changes, now)
        # Last: the trail lock its entries take is held until commit.
        if saved:
            _record_saves(session, saves.author, saved)
        if stages:
            _insert_stages(session, stages, current)
    return report


def _remove_unconfigured(configured: List[str]) -> int:
    """Mark removed every repo rule whose source is no longer configured.

    An overlay copy with a stage gets 'off', as sync_source gives a removed one,
    and a NULL row for each check it carried.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db_manager().session_scope() as session:
        removed = session.execute(
            update(DetectionRule)
            .where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.source.not_in(configured),
                DetectionRule.status != "removed",
            )
            .values(status="removed")
            .returning(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.source,
                DetectionRule.language,
            )
        ).all()
        stages = [
            _stage_row(rule_id, version, "off", "source removed")
            for rule_id, version, source, language in removed
            if source == readers.OVERLAY_SOURCE and language in STAGED_LANGUAGES
        ]
        if stages:
            _insert_stages(session, stages, _current_stages(session))
        # Every such rule with recorded checks (only overlay copies have them),
        # already removed ones too, so a check left from before is dropped.
        recorded = session.execute(
            select(
                DetectionRule.id, DetectionRule.latest_version, DetectionRule.source
            ).where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.source.not_in(configured),
                DetectionRule.id.in_(select(DetectionRuleCheckChange.rule_id)),
            )
        ).all()
        _drop_all(session, recorded, DROPPED_WHEN_DISABLED, "source removed", now)
        return len(removed)


def sync_repos() -> List[Any]:
    """Read every configured repo, Wazuh, Kibana and Splunk, and sync each, checking
    Wazuh, Kibana and Splunk rules' Quiet and Sees, then their Accurate and
    Caught.

    One failing source skips only itself.
    """
    from core.detections.detection_rules_service import DetectionRulesService

    with _sync_lock():
        service = DetectionRulesService()
        sources = service.list_sources()
        reports = [_sync_one(service, source) for source in sources]
        removed = _remove_unconfigured([readers.source_name(s) for s in sources])
        # Wazuh's searches go before Kibana's Sees backlog can spend the budget.
        wazuh_report = _sync_wazuh()
        reports.append(wazuh_report)
        if not (wazuh_report.error or wazuh_report.skipped):
            reports.append(quiet.sweep_wazuh())  # Quiet first, as for Kibana
            reports.append(wazuh_sees.sweep())
        elastic, config = _sync_elastic()
        reports.append(_drifted(_linked(elastic, _link_same_id)))
        if config is not None:
            # Quiet first: its one search is spent before Sees' groups.
            reports.append(quiet.sweep_elastic(config))
            reports.append(sees.sweep_elastic(config))
        elif (es_config := _overlay_elastic_config()) is not None:
            # No Kibana sync: Vigil's overlay copies still need Sees to promote.
            reports.append(sees.sweep_elastic(es_config, kibana_rules=False))
        splunk_report, splunk_config = _sync_splunk()
        reports.append(_drifted(_linked(splunk_report, _link_known_name)))
        if splunk_config is not None:
            # After Runs, and before Sees' searches can spend the budget.
            reports.append(quiet.sweep_splunk(splunk_config))
            reports.append(splunk_sees.sweep(splunk_config))
        reports.append(accurate.sweep())
        reports.append(caught.sweep())  # at most once a day; local reads only
    if removed:
        logger.info(
            "detection rules: %d rules of unconfigured sources removed", removed
        )
    return reports


def _overlay_elastic_config() -> Optional[Dict[str, Any]]:
    """elastic-siem's config as the overlay runner reads it (no kibana_url needed)."""
    from core.detections.inventory.rollout import _elastic_config

    try:
        return _elastic_config()
    except Exception as exc:  # noqa: BLE001 -- the Elastic sync's report stands
        logger.warning("detection rules: elastic-siem config unread: %s", exc)
        return None


def _linked(report: SourceReport, link: Callable[[], tuple]) -> SourceReport:
    """Link a SIEM source's rules after its sync; a failure only marks the report."""
    try:
        report.linked, report.unlinked = link()
    except Exception as exc:  # noqa: BLE001 -- the next sync links again
        logger.warning(
            "detection rules: linking %s rules failed: %s", report.source, exc
        )
        report.error = report.error or f"linking: {type(exc).__name__}: {exc}"
    return report


def _drifted(report: SourceReport) -> SourceReport:
    """Check drift for a SIEM source that read and linked; a failure only marks the report."""
    if report.error or report.skipped:
        return report  # unread this sync: its copies keep their drift as it was
    try:
        report.drifted = _drift(report.source)
    except Exception as exc:  # noqa: BLE001 -- the next sync checks again
        logger.warning("detection rules: drift of %s failed: %s", report.source, exc)
        report.error = f"drift: {type(exc).__name__}: {exc}"
    return report


def _sync_one(
    service: Any, source: Dict[str, Any], saves: Optional[Saves] = None
) -> SourceReport:
    name = readers.source_name(source)
    if source.get("status") != "ready":
        return SourceReport(source=name, skipped=source.get("status"))
    try:
        root = Path(
            service._under(source["local_path"], source.get("subdirectory", ""))
        )
        report = sync_source(name, readers.read_source(source, root), saves)
    except Exception as exc:  # noqa: BLE001 -- the other sources still sync
        logger.warning("detection rules: syncing %s failed: %s", name, exc)
        return SourceReport(source=name, error=f"{type(exc).__name__}: {exc}")
    logger.info("detection rules: %s", asdict(report))
    return report


def _remove_siem(source: str, integration: str) -> int:
    """Mark a SIEM source's rules removed, with a status row for each and a
    NULL row for each check they carried, drift included."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_manager().session_scope() as session:
        # Read under the sync lock, so the statuses read are the ones replaced.
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.source,
                DetectionRule.status,
            ).where(DetectionRule.source == source)
        ).all()
        # Already removed ones too, so a check left from before is dropped.
        _drop_all(
            session,
            rows,
            DROPPED_WHEN_DISABLED + ("drift",),
            f"{integration} integration not enabled",
            now,
        )
        rows = [r for r in rows if r.status != "removed"]
        if not rows:
            return 0
        session.execute(
            update(DetectionRule)
            .where(DetectionRule.id.in_([r.id for r in rows]))
            .values(status="removed")
        )
        _record_changes(
            session,
            [
                _change(
                    r.id,
                    r.latest_version,
                    "status",
                    r.status,
                    "removed",
                    f"{integration} integration not enabled",
                )
                for r in rows
            ],
            now,
        )
        return len(rows)


def _sync_elastic() -> tuple[SourceReport, Optional[Dict[str, Any]]]:
    """Kibana's detection engine, with the elastic-siem integration's credentials.

    Its rules are removed only when the integration config was read and
    elastic-siem is not enabled; a config that cannot be read skips the source.
    The config comes back only when the sync succeeded, for Quiet and Sees.
    """
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve
    from core.integrations.elastic.descriptor import ELASTIC

    try:
        integrations = load_integrations_config()
        if not integrations["configured"]:
            return (
                SourceReport(source=kibana.SOURCE, skipped="no integration config"),
                None,
            )
        if ELASTIC.id not in integrations["enabled_integrations"]:
            return (
                SourceReport(
                    source=kibana.SOURCE,
                    removed=_remove_siem(kibana.SOURCE, "elastic-siem"),
                    skipped="not enabled",
                ),
                None,
            )
        config = resolve(ELASTIC)
        absent = missing(config, "elasticsearch_url", "kibana_url")
        if absent:
            return SourceReport(source=kibana.SOURCE, skipped=f"no {absent[0]}"), None
        report = sync_source(kibana.SOURCE, kibana.read_kibana(config))
    except Exception as exc:  # noqa: BLE001 -- unreachable is skipped, never removed
        logger.warning("detection rules: syncing elastic failed: %s", exc)
        return (
            SourceReport(source=kibana.SOURCE, error=f"{type(exc).__name__}: {exc}"),
            None,
        )
    logger.info("detection rules: %s", asdict(report))
    return report, config


def _sync_wazuh() -> SourceReport:
    """The Wazuh manager's loaded rules, with the wazuh integration's credentials.

    Removed only when the integration config was read and wazuh is not enabled;
    an unreadable config or an unreachable manager skips the source.
    """
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve
    from core.integrations.wazuh.descriptor import WAZUH, WAZUH_REQUIRED

    try:
        integrations = load_integrations_config()
        if not integrations["configured"]:
            return SourceReport(source=wazuh.SOURCE, skipped="no integration config")
        if WAZUH.id not in integrations["enabled_integrations"]:
            return SourceReport(
                source=wazuh.SOURCE,
                removed=_remove_siem(wazuh.SOURCE, "wazuh"),
                skipped="not enabled",
            )
        config = resolve(WAZUH)
        absent = missing(config, *WAZUH_REQUIRED)
        if absent:
            return SourceReport(source=wazuh.SOURCE, skipped=f"no {absent[0]}")
        report = sync_source(wazuh.SOURCE, wazuh.read_wazuh(config))
    except Exception as exc:  # noqa: BLE001 -- unreachable is skipped, never removed
        logger.warning("detection rules: syncing wazuh failed: %s", exc)
        return SourceReport(source=wazuh.SOURCE, error=f"{type(exc).__name__}: {exc}")
    logger.info("detection rules: %s", asdict(report))
    return report


def _sync_splunk() -> tuple[SourceReport, Optional[Dict[str, Any]]]:
    """Splunk's saved searches, with the splunk integration's credentials.

    The same config handling as _sync_elastic: removed only when the config was
    read and splunk is not enabled; an unreadable config or Splunk is skipped.
    The config comes back only when the sync succeeded, for Sees.
    """
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve
    from core.integrations.splunk.descriptor import SPLUNK, SPLUNK_SELFHOSTED_REQUIRED

    try:
        integrations = load_integrations_config()
        if not integrations["configured"]:
            return (
                SourceReport(source=splunk.SOURCE, skipped="no integration config"),
                None,
            )
        if SPLUNK.id not in integrations["enabled_integrations"]:
            return (
                SourceReport(
                    source=splunk.SOURCE,
                    removed=_remove_siem(splunk.SOURCE, "splunk"),
                    skipped="not enabled",
                ),
                None,
            )
        config = resolve(SPLUNK)
        absent = missing(config, *SPLUNK_SELFHOSTED_REQUIRED)
        if absent:
            return SourceReport(source=splunk.SOURCE, skipped=f"no {absent[0]}"), None
        report = sync_source(splunk.SOURCE, splunk.read_splunk(config))
    except Exception as exc:  # noqa: BLE001 -- unreachable is skipped, never removed
        logger.warning("detection rules: syncing splunk failed: %s", exc)
        return (
            SourceReport(source=splunk.SOURCE, error=f"{type(exc).__name__}: {exc}"),
            None,
        )
    logger.info("detection rules: %s", asdict(report))
    return report, config


def _link_same_id() -> tuple:
    """Link each Kibana rule to the one active repo Elastic rule with its rule_id."""
    return _link(
        kibana.SOURCE,
        "elastic",
        "same_id",
        lambda copy: copy.native_key,
        DetectionRule.native_key,
        "same elastic rule_id {}",
    )


def _link_known_name() -> tuple:
    """Link each 'ESCU - <name> - Rule' search to the one active repo Splunk rule named <name>."""

    def name_of(copy: DetectionRule) -> Optional[str]:
        match = ESCU_NAME.match(copy.name or "")
        return match.group(1) if match else None

    return _link(
        splunk.SOURCE,
        "splunk",
        "known_name",
        name_of,
        DetectionRule.name,
        "ESCU saved search of the repo rule named {!r}",
    )


def _link(
    source: str,
    language: str,
    kind: str,
    key_of: Callable[[DetectionRule], Optional[str]],
    origin_key: Any,
    reason: str,
) -> tuple:
    """Link each ``source`` rule to the one active repo ``language`` rule whose
    ``origin_key`` is ``key_of(copy)``.

    Only rows the sync owns (no link, or a ``kind`` one) are written, and only
    when the origin differs. A key more than one repo rule carries links nothing;
    the overlay's rule is left out of that count when a customer repo carries it.
    Returns (linked, unlinked).
    """
    linked = unlinked = 0
    now = datetime.now(timezone.utc)
    with get_db_manager().session_scope() as session:
        origins: Dict[str, List[int]] = {}
        overlay: Dict[str, int] = {}
        for rule_id, key, rule_source in session.execute(
            select(DetectionRule.id, origin_key, DetectionRule.source).where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.language == language,
                DetectionRule.status == "active",
            )
        ):
            if rule_source == readers.OVERLAY_SOURCE:
                overlay[key] = rule_id
            else:
                origins.setdefault(key, []).append(rule_id)
        # The overlay counts only where no customer repo carries the key: once
        # its rule is merged there, the repo rule is the origin.
        for key, rule_id in overlay.items():
            origins.setdefault(key, [rule_id])
        copies = session.scalars(
            select(DetectionRule).where(
                DetectionRule.source == source,
                or_(
                    DetectionRule.link_kind.is_(None),
                    DetectionRule.link_kind == kind,
                ),
            )
            # An agent's decision on a copy commits before or after this, not between.
            .with_for_update()
        ).all()
        for copy in copies:
            key = key_of(copy) if copy.language == language else None
            ids = origins.get(key, []) if key is not None else []
            target = ids[0] if len(ids) == 1 else None
            if target == copy.origin_rule_id:
                continue
            if target is None:
                copy.origin_rule_id = copy.link_kind = copy.linked_by = None
                copy.link_reason = copy.linked_at = None
                unlinked += 1
                continue
            copy.origin_rule_id, copy.link_kind = target, kind
            copy.linked_by = "system"
            copy.link_reason = reason.format(key)
            copy.linked_at = now
            linked += 1
    return linked, unlinked


DRIFT_LINKS = ("same_id", "known_name", "agent")
SIEM_NAMES = {kibana.SOURCE: "Kibana", splunk.SOURCE: "Splunk", wazuh.SOURCE: "Wazuh"}


def _at(when: datetime) -> str:
    return f"{when.astimezone(timezone.utc):%d %b %H:%M} UTC"


def _drift_of(
    copy: DetectionRule,
    seen: datetime,
    prev_seen: Optional[datetime],
    fp: str,
    origin: Any,
    siem: str,
    linked_fp: Optional[str] = None,
    switched_off: Optional[datetime] = None,
    prebuilt: bool = False,
) -> tuple:
    """(result, reason) of a linked copy against its origin's latest version.

    Drift means the copy moved after the link (#3728 decision 4). ``linked_fp``
    is the fingerprint of its latest version first seen at or before the link;
    ``switched_off`` when its status last left active; ``prebuilt``
    an uncustomized Elastic prebuilt rule whose latest version is Elastic's.
    """
    repo = f"repo rule {origin.id} v{origin.latest_version}"
    mine = f"{siem} v{copy.latest_version}"
    if origin.status != "active":
        return "pass", f"origin not active: repo rule {origin.id} is {origin.status}"
    if copy.status != "active":
        # Only switching it off after the link counts; prebuilt rules install disabled.
        where = "removed from" if copy.status == "removed" else "disabled in"
        if switched_off is not None and (
            copy.linked_at is None or switched_off > copy.linked_at
        ):
            return "fail", (
                f"{where} {siem} {_at(switched_off)}; repo rule {origin.id} still active"
            )
        return "pass", f"{where} {siem}; never switched on since linked"
    moved = copy.linked_at is None or seen > copy.linked_at
    if linked_fp is not None and fp == linked_fp:
        how = "back to what it was when" if moved else "unchanged since"
        return "pass", f"{mine} {how} linked"
    if copy.link_kind == "same_id":
        if fp == origin.fingerprint:
            return "pass", f"{mine} matches {repo}"
        if prebuilt:
            return "pass", f"{mine} is an Elastic prebuilt upgrade, not customized"
        if moved and seen > origin.first_seen:
            return "fail", (
                f"{mine} first seen {_at(seen)}; {repo} unchanged since "
                f"{_at(origin.first_seen)}"
            )
        if origin.first_seen >= seen:
            return "pass", (
                f"repo ahead: {repo} first seen {_at(origin.first_seen)}, "
                "not deployed yet"
            )
        return "pass", f"{mine} already differed from {repo} when linked"
    # known_name / agent: the two never fingerprint equal; only the copy moving counts.
    if not moved:
        return "pass", f"{mine} unchanged since linked {_at(copy.linked_at)}"
    if prev_seen is not None and origin.first_seen > prev_seen:
        return "pass", (
            f"{repo} first seen {_at(origin.first_seen)}, after {siem}'s previous version"
        )
    return "fail", (
        f"{mine} first seen {_at(seen)}, after the link; {repo} unchanged since "
        f"{_at(origin.first_seen)}"
    )


def _drift(source: str) -> int:
    """Write checks['drift'] for ``source``'s copies; returns the change rows written."""
    now = datetime.now(timezone.utc).isoformat()
    siem = SIEM_NAMES.get(source, source)
    v = DetectionRuleVersion
    latest = and_(
        v.rule_id == DetectionRule.id, v.version == DetectionRule.latest_version
    )
    with get_db_manager().session_scope() as session:
        last = _last_results(session, source)
        copies = session.execute(
            select(
                DetectionRule,
                v.fingerprint,
                v.first_seen,
                v.checks,
                v.content["version"].astext,
                # Uncustomized Elastic prebuilt: Kibana's own record of it.
                and_(
                    v.content["immutable"].astext == "true",
                    func.coalesce(
                        v.content["rule_source"]["is_customized"].astext, "false"
                    )
                    != "true",
                ),
            )
            .join(v, latest)
            .where(DetectionRule.source == source)
            # A link decision commits before or after this, not between.
            .with_for_update(of=DetectionRule)
        ).all()
        prev = {
            r: (seen, pkg)
            for r, seen, pkg in session.execute(
                select(v.rule_id, v.first_seen, v.content["version"].astext)
                .join(DetectionRule, DetectionRule.id == v.rule_id)
                .where(
                    DetectionRule.source == source,
                    v.version == DetectionRule.latest_version - 1,
                )
            )
        }
        # Each copy's fingerprint at link time: its latest version seen by then.
        linked_fps = dict(
            session.execute(
                select(v.rule_id, v.fingerprint)
                .join(DetectionRule, DetectionRule.id == v.rule_id)
                .where(
                    DetectionRule.source == source,
                    v.first_seen <= DetectionRule.linked_at,
                )
                .distinct(v.rule_id)
                .order_by(v.rule_id, v.version.desc())
            ).all()
        )
        # When each copy last left active: for a copy off now, its last switch-off.
        ch = DetectionRuleCheckChange
        switched_off = dict(
            session.execute(
                select(ch.rule_id, ch.changed_at)
                .join(DetectionRule, DetectionRule.id == ch.rule_id)
                .where(
                    DetectionRule.source == source,
                    ch.check_key == "status",
                    ch.old_result == "active",
                )
                .distinct(ch.rule_id)
                .order_by(ch.rule_id, ch.changed_at.desc(), ch.id.desc())
            ).all()
        )
        origin_ids = {c.origin_rule_id for c, *_ in copies if c.origin_rule_id}
        origins = {
            o.id: o
            for o in session.execute(
                select(
                    DetectionRule.id,
                    DetectionRule.status,
                    DetectionRule.latest_version,
                    v.fingerprint,
                    v.first_seen,
                )
                .join(v, latest)
                .where(DetectionRule.id.in_(origin_ids))
            ).all()
        }
        changes: List[dict] = []
        checked: List[tuple] = []
        for copy, fp, seen, checks, pkg, prebuilt in copies:
            checks = checks or {}
            before = last.get((copy.id, "drift"))
            origin = origins.get(copy.origin_rule_id)
            if copy.link_kind not in DRIFT_LINKS or origin is None:
                if before and before[0] is not None:
                    changes.append(
                        _change(
                            copy.id,
                            copy.latest_version,
                            "drift",
                            before[0],
                            None,
                            "not linked",
                        )
                    )
                if "drift" in checks:
                    rest = {k: x for k, x in checks.items() if k != "drift"}
                    checked.append((copy.id, copy.latest_version, rest))
                continue
            prev_seen, prev_pkg = prev.get(copy.id, (None, None))
            new = _drift_of(
                copy,
                seen,
                prev_seen,
                fp,
                origin,
                siem,
                linked_fps.get(copy.id),
                switched_off.get(copy.id),
                # Kibana 8.17 leaves is_customized false on an edit, but only an
                # Elastic upgrade changes the rule's own version.
                source == kibana.SOURCE
                and bool(prebuilt)
                and (prev_seen is None or pkg != prev_pkg),
            )
            if before != new:
                changes.append(
                    _change(
                        copy.id,
                        copy.latest_version,
                        "drift",
                        before[0] if before else None,
                        *new,
                    )
                )
            stored = checks.get("drift") or {}
            if (stored.get("result"), stored.get("reason")) != new:
                drift = {"result": new[0], "reason": new[1], "checked_at": now}
                checked.append(
                    (copy.id, copy.latest_version, {**checks, "drift": drift})
                )
        if checked:
            session.connection().execute(
                update(v)
                .where(v.rule_id == bindparam("rid"), v.version == bindparam("ver"))
                .values(checks=bindparam("new_checks")),
                [{"rid": i, "ver": n, "new_checks": c} for i, n, c in checked],
            )
        _record_changes(session, changes, now)
    return len(changes)
