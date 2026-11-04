"""Bring the detection rule tables in line with every configured repo and Kibana.

Each run reads every ready source in full, since a full list is the only way
to see a deletion, and writes only what differs:

- a key never seen before is a new rule at version 1;
- a key whose fingerprint differs from its latest version gets the next version;
- a key the list no longer has is status 'removed', never deleted;
- a name, path, technique or tactic change updates the rule row, no new version;
- a SIEM rule switched off is status 'disabled', and carries no Runs or Sees result.

A Runs result (checks['runs'] on the latest version) is written only when its
result or reason differs from the stored one; a new version starts with none.
After Kibana syncs, ``sees`` checks whether each active Kibana rule's data is
arriving and writes checks['sees'] the same way.

After the sources, each Kibana rule is linked to the repo rule it came from when
that is certain (``same_id``: one active repo Elastic rule with its rule_id). A
link or decline an agent or person made is never touched.

A source that fails to read, or is configured but not ready, is skipped whole.
A source no longer configured has its rules marked removed.

Vigil's overlay source alone has rollout stages, for its splunk and elastic
copies (nothing runs a Sigma copy): a new rule or version, or a removed one
back unchanged, appends 'shadow'; a removed one appends 'off'.

Nothing is ever written to a SIEM.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import and_, bindparam, func, insert, or_, select, update

from core.detections.inventory import kibana, readers, sees
from core.detections.inventory.record import RuleRecord, fingerprint
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleStage, DetectionRuleVersion

logger = logging.getLogger(__name__)

# Serialises whole syncs across the daemon and API workers.
SYNC_LOCK_KEY = 0x7669_6769_6C52_554C  # "vigilRUL"
REPO_PREFIX = "repo:"
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


def _checks(stored: Dict[str, Any], record: RuleRecord, now: str) -> Dict[str, Any]:
    """``stored`` with the record's Runs result, or itself when nothing changed."""
    if record.status == "disabled":
        return {k: v for k, v in stored.items() if k not in ("runs", "sees")}
    runs, old = record.runs, stored.get("runs") or {}
    if runs is None or (
        old.get("result") == runs["result"] and old.get("reason") == runs["reason"]
    ):
        return stored
    return {**stored, "runs": {**runs, "checked_at": now}}


def sync_source(source: str, records: List[RuleRecord]) -> SourceReport:
    """Write the differences between ``records`` (all of ``source``) and the tables."""
    report = SourceReport(source=source, read=len(records))
    now = datetime.now(timezone.utc).isoformat()
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

        listed = {r.native_key for r in records}
        for key, (rule, _, _) in existing.items():
            if key not in listed and rule.status != "removed":
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
    """Read every configured repo and Kibana, and sync each, then check Kibana rules' Sees.

    One failing source skips only itself.
    """
    from core.detections.detection_rules_service import DetectionRulesService

    with _sync_lock():
        service = DetectionRulesService()
        sources = service.list_sources()
        reports = [_sync_one(service, source) for source in sources]
        removed = _remove_unconfigured([readers.source_name(s) for s in sources])
        elastic, config = _sync_elastic()
        try:
            elastic.linked, elastic.unlinked = _link_same_id()
        except Exception as exc:  # noqa: BLE001 -- the next sync links again
            logger.warning("detection rules: linking Kibana rules failed: %s", exc)
            elastic.error = elastic.error or f"linking: {type(exc).__name__}: {exc}"
        reports.append(elastic)
        if config is not None:
            reports.append(sees.sweep_elastic(config))
    if removed:
        logger.info(
            "detection rules: %d rules of unconfigured sources removed", removed
        )
    return reports


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


def _remove_elastic() -> int:
    with get_db_manager().session_scope() as session:
        result = session.execute(
            update(DetectionRule)
            .where(
                DetectionRule.source == kibana.SOURCE,
                DetectionRule.status != "removed",
            )
            .values(status="removed")
        )
        return int(getattr(result, "rowcount", 0) or 0)


def _sync_elastic() -> tuple[SourceReport, Optional[Dict[str, Any]]]:
    """Kibana's detection engine, with the elastic-siem integration's credentials.

    Its rules are removed only when the integration config was read and
    elastic-siem is not enabled; a config that cannot be read skips the source.
    The config comes back only when the sync succeeded, for the Sees sweep.
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
                    removed=_remove_elastic(),
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


def _link_same_id() -> tuple:
    """Link each Kibana rule to the one active repo Elastic rule with its rule_id.

    Only rows the sync owns (no link, or a same_id one) are written, and only
    when the origin differs. A rule_id more than one repo carries links nothing.
    Returns (linked, unlinked).
    """
    linked = unlinked = 0
    now = datetime.now(timezone.utc)
    with get_db_manager().session_scope() as session:
        origins: Dict[str, List[int]] = {}
        for rule_id, key in session.execute(
            select(DetectionRule.id, DetectionRule.native_key).where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.language == "elastic",
                DetectionRule.status == "active",
            )
        ):
            origins.setdefault(key, []).append(rule_id)
        copies = session.scalars(
            select(DetectionRule).where(
                DetectionRule.source == kibana.SOURCE,
                or_(
                    DetectionRule.link_kind.is_(None),
                    DetectionRule.link_kind == "same_id",
                ),
            )
            # An agent's decision on a copy commits before or after this, not between.
            .with_for_update()
        ).all()
        for copy in copies:
            ids = origins.get(copy.native_key, []) if copy.language == "elastic" else []
            target = ids[0] if len(ids) == 1 else None
            if target == copy.origin_rule_id:
                continue
            if target is None:
                copy.origin_rule_id = copy.link_kind = copy.linked_by = None
                copy.link_reason = copy.linked_at = None
                unlinked += 1
                continue
            copy.origin_rule_id, copy.link_kind = target, "same_id"
            copy.linked_by = "system"
            copy.link_reason = f"same elastic rule_id {copy.native_key}"
            copy.linked_at = now
            linked += 1
    return linked, unlinked
