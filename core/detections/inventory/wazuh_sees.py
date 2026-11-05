"""Sees for Wazuh rules: whether each active rule's decoder is getting events.

A rule's data source is its decoder: ``details.decoded_as``, or, when it has
none, every decoder its ``if_sid`` / ``if_matched_sid`` parents reach in the
inventory's own ``wazuh`` rows (a rule fires on any parent). Groups are not
evidence. The events are read from the Wazuh indexer behind the elastic-siem
integration, which needs only ``elasticsearch_url`` (no Kibana).

At most two searches, each a size-0 terms aggregation over the last 7 days and
each spent against elastic-siem's ``max_searches_per_hour``: the archive
(``wazuh-archives-*``: which decoders got events) and the alerts
(``wazuh-alerts-*``: which decoders and rule ids alerted). Each answer is held
in memory for 24 h; rules synced in between are judged from it. Each search is
spent under the key ``wazuh:<pattern>``, so a part another process searched
within 24 h is not searched again here: its rules keep the Sees that process
wrote. Parts due are searched oldest first. Listing the indices is metadata and
spends nothing.

- ``pass``: a decoder of the rule is in either answer, or the rule or an
  ancestor alerted;
- ``fail``: only when the archive is on (it has events in the window) and none
  of the rule's decoders got one;
- ``unknown`` otherwise: without the archive Wazuh keeps alerts only, so no
  alert proves nothing. A failed search makes its part unknown, never pass.

While a part the verdict depends on has no answer here (budget spent, or
another process holds it), the rule is left as it is. Nothing is written to the
indexer or the manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from sqlalchemy import and_, select

from core.detections.inventory import kibana, sees, wazuh
from core.integrations._base import search_budget
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

DAYS = sees.DAYS
HOLD_SECONDS = 24 * 3600.0
ARCHIVES = "wazuh-archives-*"
ALERTS = "wazuh-alerts-*"
DECODER, RULE = "decoder.name", "rule.id"
# Headroom over every decoder and rule id a manager loads.
TERMS_SIZE = 20000

RuleKey = sees.RuleKey

# (indexer url, index pattern) -> (monotonic time searched, answer): the fast path
# before asking siem_searches when the part was last searched.
held: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
# (indexer url, index pattern) whose search failed in this process: searched again
# next sync, though its keyed row (spent before the search) is recent.
failed: Set[Tuple[str, str]] = set()


@dataclass
class WazuhSeesReport:
    source: str = "wazuh:sees"
    rules: int = 0
    searched: int = 0
    deferred: int = 0
    left: int = 0
    written: int = 0
    error: Optional[str] = None


def _ids(value: Any) -> List[str]:
    """``if_sid`` as the manager lists it: a comma / space string, a list, or a number."""
    items = value if isinstance(value, list) else [value]
    return [
        p
        for item in items
        if item is not None
        for p in str(item).replace(",", " ").split()
    ]


def _lineage(rows: Dict[str, Mapping[str, Any]]):
    """Functions giving each rule id's decoders and its ancestors (itself included)."""
    decoders: Dict[str, Set[str]] = {}
    ancestors: Dict[str, Set[str]] = {}

    def walk(rid: str, stack: Tuple[str, ...]) -> None:
        if rid in decoders:
            return
        details = rows.get(rid) or {}
        own = details.get("decoded_as")
        decs: Set[str] = set()
        anc: Set[str] = {rid}
        parents = _ids(details.get("if_sid")) + _ids(details.get("if_matched_sid"))
        for parent in parents:
            if parent in stack or parent not in rows:
                continue  # a cycle, or a parent the inventory does not hold
            walk(parent, stack + (rid,))
            anc |= ancestors[parent]
            if not own:
                decs |= decoders[parent]
        if isinstance(own, str) and own:
            decs = {own}
        decoders[rid], ancestors[rid] = decs, anc

    def of(rid: str) -> Tuple[Set[str], Set[str]]:
        walk(rid, ())
        return decoders[rid], ancestors[rid]

    return of


def _rules() -> Tuple[List[Tuple[RuleKey, str]], Dict[str, Mapping[str, Any]]]:
    """The active rules' (key, native id), and every non-removed rule's details."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.native_key,
                DetectionRule.status,
                DetectionRuleVersion.content,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .where(
                DetectionRule.source == wazuh.SOURCE, DetectionRule.status != "removed"
            )
            .order_by(DetectionRule.id)
        ).all()
    active = [
        ((rid, ver), key) for rid, ver, key, status, _ in rows if status == "active"
    ]
    details = {
        key: (content or {}).get("details") or {} for _, _, key, _, content in rows
    }
    return active, details


def _unknown(reason: str, data_source: Optional[str] = None) -> Dict[str, Any]:
    return {"result": "unknown", "reason": reason, "data_source": data_source}


def _source(decs: Set[str]) -> Optional[str]:
    return f"wazuh decoder {', '.join(sorted(decs))}" if decs else None


def indexer() -> Tuple[Optional[str], Mapping[str, Any]]:
    """(why the Wazuh indexer is not read, or None; elastic-siem's resolved config).

    The gate Wazuh Sees and Wazuh Quiet share, with :func:`indices`.
    """
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve
    from core.integrations.elastic.descriptor import ELASTIC

    if ELASTIC.id not in load_integrations_config()["enabled_integrations"]:
        return "elastic-siem is not enabled, so the Wazuh indexer is not read", {}
    config = resolve(ELASTIC)
    absent = missing(config, "elasticsearch_url")
    if absent:
        return f"elastic-siem has no {absent[0]}, so the Wazuh indexer is not read", {}
    return None, config


async def indices(
    service: Any, patterns: Tuple[str, ...]
) -> Tuple[Optional[str], Dict[str, bool]]:
    """(why the indexer could not be listed, or None; whether each pattern has an
    index). Listing is metadata and spends no search."""
    names = await service.get_indices()
    if names is None:
        return f"the indexer could not be read: {service.last_indices_error}", {}
    return None, {p: any(n.startswith(p.rstrip("*")) for n in names) for p in patterns}


async def _answers(
    service: Any, url: str, per_hour: int, report: WazuhSeesReport
) -> Tuple[Optional[str], Dict[str, Any]]:
    """(why no rule can be judged, or None; pattern -> answer, an error string, or absent)."""
    why, exists = await indices(service, (ARCHIVES, ALERTS))
    if why:
        return why, {}
    if not any(exists.values()):
        return f"neither {ALERTS} nor {ARCHIVES} exists on the indexer", {}
    out: Dict[str, Any] = {}
    now = time.monotonic()
    fields = {ARCHIVES: [DECODER], ALERTS: [DECODER, RULE]}
    due: List[str] = []
    for pattern in fields:
        if not exists[pattern]:
            out[pattern] = {
                "total": 0,
                "missing": True,
                **{f: {"values": set(), "other": 0} for f in fields[pattern]},
            }
            continue
        hold = held.get((url, pattern))
        if hold and now - hold[0] < HOLD_SECONDS:
            out[pattern] = hold[1]
        else:
            due.append(pattern)

    key = {p: f"wazuh:{p}" for p in due}
    last = search_budget.last_spent(sees.INTEGRATION, key.values())
    never = datetime.min.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=HOLD_SECONDS)
    # Searched within 24 h by a process whose answer this one doesn't hold: absent,
    # so its rules are left as that process wrote them.
    due = [p for p in due if last.get(key[p], never) <= cutoff or (url, p) in failed]
    for n, pattern in enumerate(sorted(due, key=lambda p: last.get(key[p], never))):
        if not search_budget.spend(sees.INTEGRATION, per_hour, key[pattern]):
            report.deferred = len(due) - n  # their rules wait for a later sync
            break
        report.searched += 1
        try:
            answer = await service.terms_since(
                pattern, fields[pattern], DAYS, TERMS_SIZE
            )
        except Exception as exc:  # noqa: BLE001 -- that part reads unknown
            out[pattern] = f"search of {pattern} failed: {type(exc).__name__}: {exc}"
            failed.add((url, pattern))
            continue
        held[(url, pattern)] = (time.monotonic(), answer)
        failed.discard((url, pattern))
        out[pattern] = answer
    return None, out


def _verdict(
    decs: Set[str], anc: Set[str], archive: Any, alerts: Any
) -> Optional[Dict[str, Any]]:
    """The rule's Sees, or None when a part it depends on has no answer yet."""
    source = _source(decs)
    names = ", ".join(sorted(decs)) or "no decoder"
    # Any event decoded as one of the rule's decoders, alerted on or archived.
    seen = set().union(
        *(p[DECODER]["values"] for p in (archive, alerts) if isinstance(p, dict))
    )
    if decs & seen:
        return {
            "result": "pass",
            "reason": f"events decoded as {', '.join(sorted(decs & seen))} in {DAYS}d",
            "data_source": source,
        }
    if isinstance(alerts, dict):
        hit = sorted(anc & alerts[RULE]["values"], key=lambda r: (len(r), r))
        if hit:
            return {
                "result": "pass",
                "reason": f"rule {hit[0]} alerted in {DAYS}d",
                "data_source": source,
            }
    # Alerts can still pass any rule; the archive can still pass or fail one with a decoder.
    if alerts is None or (decs and archive is None):
        return None
    errors = [p for p in (archive if decs else None, alerts) if isinstance(p, str)]
    if errors:
        return _unknown("; ".join(errors), source)
    if not decs:
        return _unknown(
            "no decoder: no decoded_as on the rule or its if_sid parents, "
            f"and no alert from it or its parents in {DAYS}d",
            None,
        )
    if not archive["total"]:
        return _unknown(
            "alerts only: "
            + (
                f"no {ARCHIVES} index"
                if archive.get("missing")
                else f"{ARCHIVES} has no events in {DAYS}d"
            )
            + ", so no alert "
            f"decoded as {names} does not prove no events arrived",
            source,
        )
    if archive[DECODER]["other"]:
        return _unknown(
            f"the archive's decoder list was cut off at {TERMS_SIZE} "
            f"and does not name {names}",
            source,
        )
    return {
        "result": "fail",
        "reason": f"fail: no events decoded as {' or '.join(sorted(decs))} in {DAYS}d",
        "data_source": source,
    }


def sweep() -> WazuhSeesReport:
    """Check Sees for the active Wazuh rules against the elastic-siem indexer."""
    report = WazuhSeesReport()
    try:
        active, details = _rules()
        report.rules = len(active)
        lineage = _lineage(details)
        parts: Dict[str, Any] = {}
        why, config = indexer()
        if why is None:
            per_hour = config.get("max_searches_per_hour")
            per_hour = (
                sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
            )
            service = kibana.service_of(config)

            async def _run() -> Tuple[Optional[str], Dict[str, Any]]:
                try:
                    return await _answers(
                        service, config["elasticsearch_url"], per_hour, report
                    )
                finally:
                    await service.close()

            why, parts = asyncio.run(_run())
        results: Dict[RuleKey, Dict[str, Any]] = {}
        for key, rid in active:
            decs, anc = lineage(rid)
            if why:
                results[key] = _unknown(why, _source(decs))
                continue
            verdict = _verdict(decs, anc, parts.get(ARCHIVES), parts.get(ALERTS))
            if verdict is None:
                report.left += 1
            else:
                results[key] = verdict
        report.written = sees.write_check("sees", results, source=wazuh.SOURCE)
    except Exception as exc:  # noqa: BLE001 -- the sync's own report stands
        logger.warning("detection rules: Wazuh Sees sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Wazuh Sees %s", report)
    return report
