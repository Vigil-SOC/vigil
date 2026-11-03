"""Bring the inventory in line with one source's full rule list.

Every run reads the whole list: it is cheap, and a list is the only way to see
a deletion. Only differences are written:

- a key never seen before is a new rule at version 1;
- a key whose fingerprint differs from its latest version gets the next version;
- a key the list no longer has is status 'removed' (never deleted);
- a status, name or technique change updates the rule row, no new version.

A run with nothing changed writes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from sqlalchemy import func, insert, select

from core.detections.inventory import readers
from core.detections.inventory.record import RuleRecord, fingerprint
from core.storage.connection import get_db_manager
from core.storage.models import Rule, RuleVersion

logger = logging.getLogger(__name__)


@dataclass
class SourceReport:
    source: str
    listed: int = 0
    duplicates: int = 0
    new: int = 0
    versioned: int = 0
    removed: int = 0
    restored: int = 0
    updated: int = 0
    unchanged: int = 0
    writes: int = 0
    error: Optional[str] = None
    skipped: Optional[str] = None
    versioned_keys: List[str] = field(default_factory=list)
    removed_keys: List[str] = field(default_factory=list)


def _dedupe(records: List[RuleRecord]) -> tuple[List[RuleRecord], int]:
    """First record per native key; a source should not list one twice."""
    seen: Dict[str, RuleRecord] = {}
    for record in records:
        seen.setdefault(record.native_key, record)
    return list(seen.values()), len(records) - len(seen)


def sync_source(source: str, records: List[RuleRecord]) -> SourceReport:
    """Write the differences between ``records`` (all of ``source``) and the inventory."""
    report = SourceReport(source=source, listed=len(records))
    records, report.duplicates = _dedupe(records)

    with get_db_manager().session_scope() as session:
        latest = (
            select(Rule, RuleVersion.fingerprint)
            .join(
                RuleVersion,
                (RuleVersion.rule_id == Rule.id)
                & (RuleVersion.version == Rule.latest_version),
            )
            .where(Rule.source == source)
        )
        existing = {
            rule.native_key: (rule, fp) for rule, fp in session.execute(latest).all()
        }

        new_rules: List[RuleRecord] = []
        versions: List[dict] = []
        for record in records:
            fp = fingerprint(record)
            status = "active" if record.enabled else "disabled"
            hit = existing.get(record.native_key)
            if hit is None:
                new_rules.append(record)
                continue
            rule, latest_fp = hit
            touched = False
            if fp != latest_fp:
                rule.latest_version += 1
                versions.append(
                    {
                        "rule_id": rule.id,
                        "version": rule.latest_version,
                        "fingerprint": fp,
                        "content": record.content(),
                        "seen_in": record.seen_in or source,
                    }
                )
                report.versioned += 1
                report.versioned_keys.append(record.native_key)
                touched = True
            if rule.status != status:
                if rule.status == "removed":
                    report.restored += 1
                rule.status = status
                touched = True
            if rule.name != record.name or list(rule.techniques) != record.techniques:
                rule.name = record.name
                rule.techniques = record.techniques
                touched = True
            if touched:
                rule.last_changed = func.now()
                report.writes += 1
                if fp == latest_fp:
                    report.updated += 1
            else:
                report.unchanged += 1

        listed = {r.native_key for r in records}
        for key, (rule, _) in existing.items():
            if key not in listed and rule.status != "removed":
                rule.status = "removed"
                rule.last_changed = func.now()
                report.removed += 1
                report.removed_keys.append(key)
                report.writes += 1

        if new_rules:
            ids = session.execute(
                insert(Rule).returning(
                    Rule.id, Rule.native_key, sort_by_parameter_order=True
                ),
                [
                    {
                        "name": r.name,
                        "source": source,
                        "native_key": r.native_key,
                        "techniques": r.techniques,
                        "latest_version": 1,
                        "status": "active" if r.enabled else "disabled",
                    }
                    for r in new_rules
                ],
            ).all()
            for (rule_id, _), record in zip(ids, new_rules):
                versions.append(
                    {
                        "rule_id": rule_id,
                        "version": 1,
                        "fingerprint": fingerprint(record),
                        "content": record.content(),
                        "seen_in": record.seen_in or source,
                    }
                )
            report.new = len(new_rules)
            report.writes += 2 * len(new_rules)
        if versions:
            session.execute(insert(RuleVersion), versions)
            report.writes += report.versioned
    return report


def _sources() -> List[tuple[str, Callable[[], List[RuleRecord]]]]:
    sources: List[tuple[str, Callable[[], List[RuleRecord]]]] = [
        ("splunk", readers.read_splunk),
        ("elastic", readers.read_kibana),
        ("wazuh", readers.read_wazuh),
    ]
    for repo in readers.repo_sources():
        sources.append(
            (readers.source_name(repo), lambda repo=repo: readers.read_repo(repo))
        )
    return sources


def sync_all(only: Optional[List[str]] = None) -> List[SourceReport]:
    """Read every configured source and sync it. One failing source skips only itself."""
    reports: List[SourceReport] = []
    for name, read in _sources():
        if only and name not in only:
            continue
        try:
            records = read()
        except readers.SourceNotConfigured:
            reports.append(SourceReport(source=name, skipped="not configured"))
            continue
        except Exception as exc:  # noqa: BLE001 -- the other sources still sync
            logger.warning("rule inventory: reading %s failed: %s", name, exc)
            reports.append(
                SourceReport(source=name, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        report = sync_source(name, records)
        logger.info("rule inventory: %s", asdict(report))
        reports.append(report)
    return reports
