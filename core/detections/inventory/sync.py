"""Bring the detection rule tables in line with every configured repo, Kibana, Splunk and Wazuh.

Each run reads every ready source in full, since a full list is the only way
to see a deletion, and writes only what differs:

- a key never seen before is a new rule at version 1;
- a key whose fingerprint differs from its latest version gets the next version;
- a key the list no longer has is status 'removed', never deleted;
- a name, path, technique or tactic change updates the rule row, no new version;
- a SIEM rule switched off is status 'disabled', and carries no Runs, Sees,
  Quiet, Noisy or Silent result (its drift result stays).

A Runs result (checks['runs'] on the latest version) is written only when its
result or reason differs from the stored one; a new version starts with none.
After Wazuh syncs, ``quiet`` counts each active Wazuh rule's alerts against a
per-day threshold (checks['quiet'], from ``wazuh-alerts-*`` on the elastic-siem
indexer), then ``wazuh_sees`` checks whether each one's decoder gets events;
after Kibana syncs, ``quiet`` counts each active Kibana rule's alerts the same
way and against the rule's own 30 days (checks['noisy'], checks['silent']),
then ``sees`` checks whether each active Kibana rule's data is arriving; after
Splunk syncs, ``splunk_sees`` checks each active saved search's data the same
way. All three Sees checks write checks['sees']; each check is written the
same way.

After the sources, each SIEM rule is linked to the repo rule it came from when
that is certain: a Kibana rule by ``same_id`` (one active repo Elastic rule with
its rule_id), a Splunk saved search by ``known_name`` ('ESCU - <name> - Rule' and
one active repo Splunk rule named <name>). A link or decline an agent or person
made is never touched.

Every change of a check result is also appended to detection_rule_check_changes
(``_record_changes``): a Runs result that differs from the rule's last recorded
one, across versions, and a SIEM rule's status moving between active, disabled
and removed. Repo rules have no checks and get no rows here; Vigil's Elastic
overlay copies get their Runs from ``rollout``, which records them the same way.

After Kibana and Splunk link, ``_drift`` checks each linked copy against its
repo rule and writes checks['drift'] the same way: fail when the copy is disabled
or removed while its repo rule is active, or when only the copy moved (a same_id
copy whose fingerprint differs and whose latest version is newer than both the
link and the repo rule's; a known_name or agent copy that moved after the link
with no repo version since its previous one). A copy that had a drift result and
is no longer linked gets a NULL row, reason 'not linked'. A disabled rule keeps
its drift; only Runs, Sees, Quiet, Noisy and Silent are dropped.

A source that fails to read, or is configured but not ready, is skipped whole.
A source no longer configured has its rules marked removed.

Vigil's overlay source alone has rollout stages, for its splunk and elastic
copies (nothing runs a Sigma copy): a new rule or version, or a removed one
back unchanged, appends 'shadow'; a removed one appends 'off'.

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

from sqlalchemy import and_, bindparam, func, insert, or_, select, update

from core.detections.inventory import (
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


def _stage_row(rule_id: int, version: int, stage: str, reason: str) -> dict:
    return {
        "rule_id": rule_id,
        "version": version,
        "stage": stage,
        "actor": "system",
        "reason": reason,
    }


def _rule_fields(record: RuleRecord) -> Dict[str, Any]:
    return {
        "name": record.name,
        "language": record.language,
        "techniques": record.techniques,
        "tactics": record.tactics,
        "path": record.path,
    }


DROPPED_WHEN_DISABLED = ("runs", "sees", "quiet", "noisy", "silent")


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


def _record_changes(session: Any, rows: List[dict], now: str) -> None:
    """Append check result changes, in the caller's transaction. Every writer uses it."""
    if rows:
        session.execute(
            insert(DetectionRuleCheckChange),
            [{**r, "changed_at": datetime.fromisoformat(now)} for r in rows],
        )


def sync_source(source: str, records: List[RuleRecord]) -> SourceReport:
    """Write the differences between ``records`` (all of ``source``) and the tables."""
    report = SourceReport(source=source, read=len(records))
    now = datetime.now(timezone.utc).isoformat()
    siem = not source.startswith(REPO_PREFIX)
    stages: List[dict] = []

    def stage(rule_id: int, version: int, language: str, to: str, reason: str):
        if source == readers.OVERLAY_SOURCE and language in STAGED_LANGUAGES:
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
        last = _last_results(session, source) if siem else {}
        changes: List[dict] = []

        new: List[tuple] = []
        versions: List[dict] = []
        # (rule id, version, checks) to write once the versions exist.
        checked: List[tuple] = []
        for record in records:
            fp = fingerprint(record)
            hit = existing.get(record.native_key)
            if hit is None:
                new.append((record, fp))
                continue
            rule, latest_fp, stored = hit
            if fp != latest_fp:
                rule.latest_version += 1
                versions.append(_version_row(rule.id, rule.latest_version, record, fp))
                report.versioned += 1
                stored = {}
                stage(
                    rule.id,
                    rule.latest_version,
                    record.language,
                    "shadow",
                    "new version",
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
                        )
                else:
                    changed["status"] = record.status
                rule.status = record.status
            for key, value in changed.items():
                setattr(rule, key, value)
            if changed and fp == latest_fp:
                report.updated += 1
            checks = _checks(stored, record, now)
            if checks != stored:
                checked.append((rule.id, rule.latest_version, checks))
            if siem:
                runs = _runs_change(rule.id, rule.latest_version, record, last)
                if runs:
                    changes.append(runs)

        listed = {r.native_key for r in records}
        for key, (rule, _, _) in existing.items():
            if key not in listed and rule.status != "removed":
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
                stage(rule_id, 1, r.language, "shadow", "new rule")
                checks = _checks({}, r, now)
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
        if stages:
            session.execute(insert(DetectionRuleStage), stages)
        if checked:
            # checks is the one column of a version ever updated.
            session.connection().execute(
                update(DetectionRuleVersion)
                .where(
                    DetectionRuleVersion.rule_id == bindparam("rid"),
                    DetectionRuleVersion.version == bindparam("ver"),
                )
                .values(checks=bindparam("new_checks")),
                [{"rid": i, "ver": v, "new_checks": c} for i, v, c in checked],
            )
            report.checked = len(checked)
        _record_changes(session, changes, now)
    return report


def _remove_unconfigured(configured: List[str]) -> int:
    """Mark removed every repo rule whose source is no longer configured.

    An overlay copy with a stage gets 'off', as sync_source gives a removed one.
    """
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
            session.execute(insert(DetectionRuleStage), stages)
        return len(removed)


def sync_repos() -> List[Any]:
    """Read every configured repo, Wazuh, Kibana and Splunk, and sync each, checking Wazuh, Kibana and Splunk rules' Quiet and Sees.

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
        splunk_report, splunk_config = _sync_splunk()
        reports.append(_drifted(_linked(splunk_report, _link_known_name)))
        if splunk_config is not None:
            # After Runs, and before Sees' searches can spend the budget.
            reports.append(quiet.sweep_splunk(splunk_config))
            reports.append(splunk_sees.sweep(splunk_config))
    if removed:
        logger.info(
            "detection rules: %d rules of unconfigured sources removed", removed
        )
    return reports


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


def _sync_one(service: Any, source: Dict[str, Any]) -> SourceReport:
    name = readers.source_name(source)
    if source.get("status") != "ready":
        return SourceReport(source=name, skipped=source.get("status"))
    try:
        root = Path(
            service._under(source["local_path"], source.get("subdirectory", ""))
        )
        report = sync_source(name, readers.read_source(source, root))
    except Exception as exc:  # noqa: BLE001 -- the other sources still sync
        logger.warning("detection rules: syncing %s failed: %s", name, exc)
        return SourceReport(source=name, error=f"{type(exc).__name__}: {exc}")
    logger.info("detection rules: %s", asdict(report))
    return report


def _remove_siem(source: str, integration: str) -> int:
    """Mark a SIEM source's rules removed, with a status row for each."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_manager().session_scope() as session:
        # Read under the sync lock, so the statuses read are the ones replaced.
        rows = session.execute(
            select(
                DetectionRule.id, DetectionRule.latest_version, DetectionRule.status
            ).where(
                DetectionRule.source == source,
                DetectionRule.status != "removed",
            )
        ).all()
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
    when the origin differs. A key more than one repo rule carries links nothing.
    Returns (linked, unlinked).
    """
    linked = unlinked = 0
    now = datetime.now(timezone.utc)
    with get_db_manager().session_scope() as session:
        origins: Dict[str, List[int]] = {}
        for rule_id, key in session.execute(
            select(DetectionRule.id, origin_key).where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.language == language,
                DetectionRule.status == "active",
            )
        ):
            origins.setdefault(key, []).append(rule_id)
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
SIEM_NAMES = {kibana.SOURCE: "Kibana", splunk.SOURCE: "Splunk"}


def _at(when: datetime) -> str:
    return f"{when.astimezone(timezone.utc):%d %b %H:%M} UTC"


def _drift_of(
    copy: DetectionRule,
    seen: datetime,
    prev_seen: Optional[datetime],
    fp: str,
    origin: Any,
    siem: str,
) -> tuple:
    """(result, reason) of a linked copy against its origin's latest version."""
    repo = f"repo rule {origin.id} v{origin.latest_version}"
    mine = f"{siem} v{copy.latest_version}"
    if origin.status != "active":
        return "pass", f"origin not active: repo rule {origin.id} is {origin.status}"
    if copy.status != "active":
        where = "removed from" if copy.status == "removed" else "disabled in"
        return "fail", f"{where} {siem}; repo rule {origin.id} still active"
    moved = copy.linked_at is None or seen > copy.linked_at
    if copy.link_kind == "same_id":
        if fp == origin.fingerprint:
            return "pass", f"{mine} matches {repo}"
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
            select(DetectionRule, v.fingerprint, v.first_seen, v.checks)
            .join(v, latest)
            .where(DetectionRule.source == source)
            # A link decision commits before or after this, not between.
            .with_for_update(of=DetectionRule)
        ).all()
        prev = dict(
            session.execute(
                select(v.rule_id, v.first_seen)
                .join(DetectionRule, DetectionRule.id == v.rule_id)
                .where(
                    DetectionRule.source == source,
                    v.version == DetectionRule.latest_version - 1,
                )
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
        for copy, fp, seen, checks in copies:
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
            new = _drift_of(copy, seen, prev.get(copy.id), fp, origin, siem)
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
