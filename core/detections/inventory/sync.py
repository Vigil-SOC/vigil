"""Bring the detection rule tables in line with every configured repo.

Each run reads every ready source in full, since a full list is the only way
to see a deletion, and writes only what differs:

- a key never seen before is a new rule at version 1;
- a key whose fingerprint differs from its latest version gets the next version;
- a key the list no longer has is status 'removed', never deleted;
- a name, path, technique or tactic change updates the rule row, no new version.

A source that fails to read, or is configured but not ready, is skipped whole.
A source no longer configured has its rules marked removed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import and_, func, insert, select, update

from core.detections.inventory import readers
from core.detections.inventory.record import RuleRecord, fingerprint
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

# Serialises whole syncs across the daemon and API workers.
SYNC_LOCK_KEY = 0x7669_6769_6C52_554C  # "vigilRUL"
REPO_PREFIX = "repo:"


@dataclass
class SourceReport:
    source: str
    read: int = 0
    new: int = 0
    versioned: int = 0
    removed: int = 0
    restored: int = 0
    updated: int = 0
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


def _rule_fields(record: RuleRecord) -> Dict[str, Any]:
    return {
        "name": record.name,
        "language": record.language,
        "techniques": record.techniques,
        "tactics": record.tactics,
        "path": record.path,
    }


def sync_source(source: str, records: List[RuleRecord]) -> SourceReport:
    """Write the differences between ``records`` (all of ``source``) and the tables."""
    report = SourceReport(source=source, read=len(records))
    with get_db_manager().session_scope() as session:
        latest = (
            select(DetectionRule, DetectionRuleVersion.fingerprint)
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
            rule.native_key: (rule, fp) for rule, fp in session.execute(latest).all()
        }

        new: List[tuple] = []
        versions: List[dict] = []
        for record in records:
            fp = fingerprint(record)
            hit = existing.get(record.native_key)
            if hit is None:
                new.append((record, fp))
                continue
            rule, latest_fp = hit
            if fp != latest_fp:
                rule.latest_version += 1
                versions.append(_version_row(rule.id, rule.latest_version, record, fp))
                report.versioned += 1
            if rule.status != "active":
                rule.status = "active"
                report.restored += 1
            changed = {
                k: v for k, v in _rule_fields(record).items() if getattr(rule, k) != v
            }
            for key, value in changed.items():
                setattr(rule, key, value)
            if changed and fp == latest_fp:
                report.updated += 1

        listed = {r.native_key for r in records}
        for key, (rule, _) in existing.items():
            if key not in listed and rule.status != "removed":
                rule.status = "removed"
                report.removed += 1

        if new:
            ids = session.scalars(
                insert(DetectionRule).returning(
                    DetectionRule.id, sort_by_parameter_order=True
                ),
                [
                    {
                        "source": source,
                        "native_key": r.native_key,
                        "status": "active",
                        "latest_version": 1,
                        **_rule_fields(r),
                    }
                    for r, _ in new
                ],
            ).all()
            versions.extend(
                _version_row(rule_id, 1, r, fp) for rule_id, (r, fp) in zip(ids, new)
            )
            report.new = len(new)
        if versions:
            session.execute(insert(DetectionRuleVersion), versions)
    return report


def _remove_unconfigured(configured: List[str]) -> int:
    """Mark removed every repo rule whose source is no longer configured."""
    with get_db_manager().session_scope() as session:
        result = session.execute(
            update(DetectionRule)
            .where(
                DetectionRule.source.startswith(REPO_PREFIX),
                DetectionRule.source.not_in(configured),
                DetectionRule.status != "removed",
            )
            .values(status="removed")
        )
        return int(getattr(result, "rowcount", 0) or 0)


def sync_repos() -> List[SourceReport]:
    """Read every configured repo and sync it. One failing source skips only itself."""
    from core.detections.detection_rules_service import DetectionRulesService

    with _sync_lock():
        service = DetectionRulesService()
        sources = service.list_sources()
        reports = [_sync_one(service, source) for source in sources]
        removed = _remove_unconfigured([readers.source_name(s) for s in sources])
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
