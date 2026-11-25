"""The import check: whether a held repo rule's data is in a SIEM Vigil reads.

``sync`` writes checks['import'] 'pending' on each new rule or version of a repo
or local source (not the overlay; never a SIEM copy). ``sweep``, run by the
daemon after ``sync_repos``, judges those versions (and 'failed' ones, and
'unchecked' ones while the check is on), reading each SIEM's field list once:

- Sigma: passed when every field its detection reads is in one SIEM's list,
  matched as ``siem_replay`` matches names (case variants, snake case, any
  parent path). Elastic and Wazuh lists come from ``_field_caps`` (metadata, no
  budget); Splunk's from one ``fieldsummary`` search, spent from its budget. A
  spent budget, or no SIEM configured at all, leaves them pending for a later
  sweep.
- Elastic: Vigil reads Elastic, and the rule's index and required fields are in
  its mapping, as ``sees`` reads them.
- Splunk, Wazuh: Vigil reads that SIEM.
- KQL: flagged; it counts once a person approves it.
- A Sigma correlation, an Elastic machine learning rule, anything else: flagged,
  no check can judge it.
- Lint failing: flagged with the lint reason; approval cannot pass it.

'failed' means a SIEM read errored; it is judged again next sweep. With
VIGIL_RULE_IMPORT_CHECK off, the sweep turns pending and failed versions
'unchecked' (they count). ``approve`` lets a person make the latest version
count, audited with them as actor. Nothing is replayed and nothing is written
to a SIEM.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml
from sqlalchemy import and_, select, tuple_

from core.detections.engineering import siem_replay
from core.detections.inventory import catalog, readers
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

KQL_REASON = "Vigil reads Sentinel and Defender alerts, not the tables this query reads"
NO_CHECK = "no check can judge it"
SPLUNK_SAMPLE = 10000
SPLUNK_DAYS = 7
WAZUH_INDICES = "wazuh-archives-*,wazuh-alerts-*"
APPROVED = "detections.rule_approved"
AUDIT_TYPE = "detection_rules"


@dataclass
class ImportCheckReport:
    source: str = "repo:import"
    judged: int = 0
    passed: int = 0
    flagged: int = 0
    failed: int = 0
    left_pending: int = 0
    unchecked: int = 0
    # Verdicts written: fewer than judged when an approval or a sync came between.
    written: int = 0
    error: Optional[str] = None


@dataclass
class FieldList:
    """One SIEM's field list for the sweep: every dotted path and each of its
    tails (``a.b.c`` -> ``a.b.c``, ``b.c``, ``c``), or why it was not read."""

    siem: str
    where: str = ""
    tails: Optional[Set[str]] = None
    error: Optional[str] = None
    deferred: Optional[str] = None

    @classmethod
    def of(cls, siem: str, where: str, paths: Any) -> "FieldList":
        tails: Set[str] = set()
        for path in paths:
            parts = str(path).split(".")
            tails.update(".".join(parts[i:]) for i in range(len(parts)))
        return cls(siem, where, tails)

    def holds(self, name: str) -> bool:
        snake = siem_replay._snake(name)
        names = {*siem_replay._variants(name), snake, snake.replace("_", "-")}
        return bool(self.tails and names & self.tails)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(exc: Exception) -> str:
    return f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"


def _plain(items: List[str]) -> str:
    return siem_replay._plain_list(items) if items else ""


# --- reading the SIEMs ---------------------------------------------------------


async def _elastic_lists(config: Mapping[str, Any], wazuh: bool) -> List[FieldList]:
    """Elastic's (and, when the indexer is read, Wazuh's) list, from _field_caps."""
    from core.detections.inventory import kibana

    service = kibana.service_of(config)
    out: List[FieldList] = []
    try:
        wanted = [("elastic", siem_replay.ELASTIC_INDICES)]
        if wazuh:
            wanted.append(("wazuh", WAZUH_INDICES))
        for siem, index in wanted:
            try:
                paths = await service.field_caps(index, ["*"])
                out.append(FieldList.of(siem, index, paths))
            except Exception as exc:  # noqa: BLE001 -- judged again next sweep
                out.append(FieldList(siem, index, error=_first_line(exc)))
    finally:
        await service.close()
    return out


def _splunk_list(config: Mapping[str, Any]) -> FieldList:
    """Splunk's list from one search of the last week's events, spent from its budget."""
    from core.detections.inventory import splunk
    from core.integrations._base import search_budget
    from core.integrations.splunk.descriptor import SPLUNK

    where = f"index={siem_replay.SPLUNK_INDEX}"
    if not search_budget.spend(
        SPLUNK.id, splunk.per_hour_of(config), "import-check:fields"
    ):
        return FieldList(
            "splunk", where, deferred="its search budget for this hour is spent"
        )
    try:
        found = splunk.service_of(config).search(
            f"search {where} | head {SPLUNK_SAMPLE} | fieldsummary | fields field",
            earliest_time=f"-{SPLUNK_DAYS}d",
            max_count=0,
            all_fields=True,
        )
    except Exception as exc:  # noqa: BLE001 -- judged again next sweep
        return FieldList("splunk", where, error=_first_line(exc))
    return FieldList.of(
        "splunk", where, [r.get("field") for r in found.results if r.get("field")]
    )


def _field_lists(
    elastic: Optional[Mapping[str, Any]], splunk: Optional[Mapping[str, Any]]
) -> List[FieldList]:
    from core.detections.inventory import wazuh_sees

    lists: List[FieldList] = []
    if elastic is not None:
        why, _ = wazuh_sees.indexer()
        lists += asyncio.run(_elastic_lists(elastic, why is None))
    if splunk is not None:
        lists.append(_splunk_list(splunk))
    return lists


# --- judging one version -------------------------------------------------------


def _flagged(reason: str, **more: Any) -> Dict[str, Any]:
    return {"result": "flagged", "reason": reason, **more}


def _sigma(
    content: Mapping[str, Any], lists: List[FieldList]
) -> Optional[Dict[str, Any]]:
    """A Sigma rule's verdict; None leaves it pending (a SIEM's budget is spent)."""
    detection = content.get("detection")
    if content.get("correlation") and not detection:
        return _flagged(f"a Sigma correlation: {NO_CHECK}")
    fields = siem_replay.rule_fields(yaml.safe_dump({"detection": detection}))
    if not fields:
        return _flagged(f"its detection names no field (keywords only): {NO_CHECK}")
    read = [fl for fl in lists if fl.tails]
    for fl in read:
        if all(fl.holds(f) for f in fields):
            return {
                "result": "passed",
                "reason": f"{fl.siem.title()} ({fl.where}) holds {_plain(fields)}",
                "siem": fl.siem,
            }
    errors = [f"{fl.siem.title()}: {fl.error}" for fl in lists if fl.error]
    if errors:
        return {"result": "failed", "reason": "could not read " + "; ".join(errors)}
    if not lists or any(fl.deferred for fl in lists):
        # No SIEM configured yet, or a budget spent: judged on a later sweep.
        return None
    if not read:
        return _flagged("no SIEM Vigil reads (Elastic, Wazuh, Splunk) holds any field")
    absent = {fl.siem: [f for f in fields if not fl.holds(f)] for fl in read}
    parts = [
        f"{_plain(names)} not in {siem.title()}'s fields"
        for siem, names in absent.items()
    ]
    return _flagged("; ".join(parts), absent_fields=absent)


async def _elastic(
    service: Any,
    content: Mapping[str, Any],
    caps: Dict[Tuple[str, Tuple[str, ...]], Any],
) -> Dict[str, Any]:
    """An Elastic rule's index and required fields in the mapping, as Sees reads them."""
    from core.detections.inventory import sees
    from core.integrations._base.search_params import validate_index

    rule = sees._overlay_rule(content)
    if rule.get("type") == "machine_learning":
        return _flagged(f"a machine learning rule: {NO_CHECK}")
    patterns, why = await sees._patterns(service, rule, {})
    if why:
        if why.startswith("data view ") and " could not be read: " in why:
            return {"result": "failed", "reason": why}  # Kibana's error: retried
        return _flagged(f"{why}: {NO_CHECK}")
    index = ",".join(patterns)
    try:
        validate_index(index)
    except ValueError as exc:
        return _flagged(str(exc))
    ts = rule.get("timestamp_override") or "@timestamp"
    wanted = tuple(sorted({ts, *sees._fields(rule)}))
    if (index, wanted) not in caps:
        try:
            caps[(index, wanted)] = await service.field_caps(index, list(wanted))
        except Exception as exc:  # noqa: BLE001 -- judged again next sweep
            caps[(index, wanted)] = exc
    present = caps[(index, wanted)]
    if isinstance(present, Exception):
        return {
            "result": "failed",
            "reason": f"field caps of {index}: {_first_line(present)}",
        }
    if ts not in present:
        return _flagged(
            f"no index matching {index} maps {ts}: Elastic holds none of its events"
        )
    missing = [f for f in wanted if f not in present]
    if missing:
        return _flagged(
            f"{_plain(missing)} not in the mapping of {index}",
            absent_fields={"elastic": missing},
        )
    return {
        "result": "passed",
        "reason": f"Elastic maps {index} and its fields",
        "siem": "elastic",
    }


def _lint_reason(checks: Mapping[str, Any]) -> Optional[str]:
    lint = checks.get("lint") or {}
    if lint.get("result") != "fail":
        return None
    literals = ", ".join(
        f"{f.get('kind')} {f.get('value')}" for f in lint.get("findings") or []
    )
    return f"fails lint: keyed to {literals}" if literals else "fails lint"


# --- the sweep -----------------------------------------------------------------


def _held_rows(states: Tuple[str, ...]) -> List[Any]:
    """(id, latest version, language, content, checks) of listed repo rules whose
    latest version's import check is one of ``states``."""
    with get_db_manager().session_scope() as session:
        return session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.language,
                DetectionRuleVersion.content,
                DetectionRuleVersion.checks,
            )
            .join(DetectionRuleVersion, catalog._latest_join())
            .where(
                catalog.LISTED,
                DetectionRule.source.startswith(catalog.REPO_PREFIX),
                DetectionRule.source != readers.OVERLAY_SOURCE,
                catalog.IMPORT.in_(states),
            )
            .order_by(DetectionRule.id)
        ).all()


def _write(verdicts: Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]]) -> int:
    """Write each (rule, version)'s verdict, only where its import check still reads
    what was judged (an approval or a newer sync in between wins)."""
    from core.detections.inventory.sync import _write_checks

    if not verdicts:
        return 0
    v = DetectionRuleVersion
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(v.rule_id, v.version, v.checks)
            .where(tuple_(v.rule_id, v.version).in_(list(verdicts)))
            .with_for_update()
        ).all()
        checked = []
        for rule_id, version, checks in rows:
            was, verdict = verdicts[(rule_id, version)]
            if ((checks or {}).get("import") or {}).get("result") == was:
                checked.append(
                    (rule_id, version, {**(checks or {}), "import": verdict})
                )
        _write_checks(session, checked)
    return len(checked)


def sweep() -> ImportCheckReport:
    """Judge the held repo versions (or, switched off, let them count unchecked)."""
    from core.config import get_settings
    from core.detections.inventory import rollout

    report = ImportCheckReport()
    try:
        now = _now()
        verdicts: Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]] = {}
        if not get_settings().vigil_rule_import_check:
            for rid, ver, _, _, checks in _held_rows(("pending", "failed")):
                was = checks["import"]["result"]
                verdicts[(rid, ver)] = (
                    was,
                    {
                        "result": "unchecked",
                        "reason": "the import check is switched off",
                        "at": now,
                    },
                )
            report.unchecked = _write(verdicts)
            return report
        rows = _held_rows(("pending", "failed", "unchecked"))
        if not rows:
            return report
        elastic, splunk = rollout._elastic_config(), rollout._splunk_config()
        # Each SIEM's list is read once, and only when a rule needs it: Splunk's
        # search is spent for Sigma rules alone.
        needs = {lang for _, _, lang, _, checks in rows if not _lint_reason(checks)}
        lists = (
            _field_lists(elastic, splunk if "sigma" in needs else None)
            if needs & {"sigma", "wazuh"}
            else []
        )
        read_wazuh = any(fl.siem == "wazuh" and fl.tails for fl in lists)
        elastic_rules = [
            (r[0], r[1], r[3])
            for r in rows
            if r[2] == "elastic" and not _lint_reason(r[4])
        ]
        by_elastic: Dict[Tuple[int, int], Dict[str, Any]] = {}
        if elastic_rules and elastic is not None:
            by_elastic = asyncio.run(_judge_elastic(elastic, elastic_rules))
        for rid, ver, language, content, checks in rows:
            content = content or {}
            lint = _lint_reason(checks)
            if lint:
                verdict = _flagged(f"{lint}; approval cannot pass it", lint_fails=True)
            elif language == "sigma":
                verdict = _sigma(content, lists)
            elif language == "elastic":
                verdict = by_elastic.get((rid, ver)) or _flagged(
                    "Vigil does not read Elastic"
                )
            elif language == "splunk":
                verdict = (
                    {
                        "result": "passed",
                        "reason": "Vigil reads Splunk",
                        "siem": "splunk",
                    }
                    if splunk is not None
                    else _flagged("Vigil does not read Splunk")
                )
            elif language == "wazuh":
                verdict = (
                    {
                        "result": "passed",
                        "reason": "Vigil reads the Wazuh indexer",
                        "siem": "wazuh",
                    }
                    if read_wazuh
                    else _flagged("Vigil does not read a Wazuh indexer holding events")
                )
            elif language == "kql":
                verdict = _flagged(KQL_REASON)
            else:
                verdict = _flagged(f"a {language} rule: {NO_CHECK}")
            if verdict is None:
                report.left_pending += 1
                continue
            report.judged += 1
            setattr(report, verdict["result"], getattr(report, verdict["result"]) + 1)
            verdicts[(rid, ver)] = (
                checks["import"]["result"],
                {**verdict, "checked_at": now},
            )
        report.written = _write(verdicts)
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: import check failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: import check %s", report)
    return report


async def _judge_elastic(
    config: Mapping[str, Any], rules: List[Tuple[int, int, Mapping[str, Any]]]
) -> Dict[Tuple[int, int], Dict[str, Any]]:
    from core.detections.inventory import kibana

    service = kibana.service_of(config)
    caps: Dict[Tuple[str, Tuple[str, ...]], Any] = {}
    try:
        return {
            (rid, ver): await _elastic(service, content or {}, caps)
            for rid, ver, content in rules
        }
    finally:
        await service.close()


# --- a person's approval -------------------------------------------------------


def approve(rule_id: int, who: Tuple[str, str], reason: str) -> Dict[str, Any]:
    """Make a held repo rule's latest version count as coverage. ``who`` is
    (username, principal id). Refused (ValueError) when its lint fails, when the
    rule is not a repo or local one, or when the version is not held; a new
    version starts pending again."""
    from core.audit import trail
    from core.detections.inventory.sync import _write_checks
    from core.storage.config_service import ConfigService

    username, principal = who
    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None or rule.status not in ("active", "disabled"):
            raise LookupError(f"No detection rule {rule_id}")
        if (
            not rule.source.startswith(catalog.REPO_PREFIX)
            or rule.source == readers.OVERLAY_SOURCE
        ):
            raise ValueError(
                f"Rule {rule_id} is not a repo or local rule: only those are held"
            )
        version = session.execute(
            select(DetectionRuleVersion)
            .where(
                and_(
                    DetectionRuleVersion.rule_id == rule.id,
                    DetectionRuleVersion.version == rule.latest_version,
                )
            )
            .with_for_update()
        ).scalar_one()
        checks = dict(version.checks or {})
        lint = _lint_reason(checks)
        if lint:
            raise ValueError(
                f"Rule {rule_id} v{version.version} {lint}: approval cannot pass a lint failure"
            )
        was = checks.get("import") or {}
        state = was.get("result")
        if state not in (*catalog.IMPORT_HELD, "unchecked"):
            raise ValueError(
                f"Rule {rule_id} v{version.version} is not held"
                + (
                    f" (its import check is {state})"
                    if state
                    else " (stored before the hold)"
                )
            )
        now = _now()
        checks["import"] = {
            "result": "approved",
            "by": username,
            "at": now,
            "reason": reason,
            "was": was,
        }
        _write_checks(session, [(rule.id, version.version, checks)])
        entry = {
            "rule_id": rule.id,
            "version": version.version,
            "was": state,
            "reason": reason,
        }
        if trail.in_use(session):
            trail.append(
                session,
                {
                    trail.ACTION: APPROVED,
                    trail.ACTOR: principal,
                    trail.OBJECT: str(rule.id),
                    "username": username,
                    **entry,
                },
            )
        else:
            ConfigService(user_id=principal)._create_audit_log(
                session,
                config_type=AUDIT_TYPE,
                config_key=f"detection_rule:{rule.id}",
                action="update",
                old_value={"rule_id": rule.id, "import": state},
                new_value={
                    "rule_id": rule.id,
                    "import": "approved",
                    "event": "approve",
                },
                change_reason=reason,
            )
    # Committed: the rules list reads the approval on its next read.
    catalog.listing_changed()
    return {"success": True, **entry, "import": checks["import"]}
