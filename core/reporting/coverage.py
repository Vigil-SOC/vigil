"""Detection coverage on the Day 1 Report.

The checks are the reads the Rules and Coverage screens already show. Before
them, Quiet then Sees run in the order ``sync.sync_repos`` uses, and stop
when the hour's search budget is spent. This does not sync repos, and it does
not read demo findings.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import and_, func, select

from core.config import load_integrations_config
from core.detections.gaps import gap_report, read_alert_labels
from core.detections.inventory import (
    catalog,
    kibana,
    quiet,
    sees,
    splunk,
    splunk_sees,
    wazuh,
    wazuh_sees,
)
from core.detections.inventory.coverage import coverage as technique_coverage
from core.detections.inventory.sync import SYNC_LOCK_KEY
from core.integrations._base.config import missing, resolve
from core.integrations.elastic.descriptor import ELASTIC
from core.integrations.splunk.descriptor import SPLUNK, SPLUNK_SELFHOSTED_REQUIRED
from core.reporting.checks import spec_for
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

logger = logging.getLogger(__name__)

PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not_checked"

# Kibana Quiet and Sees need both URLs, as sync.sync_repos does before it
# calls them. Splunk's are the fields its own sweep reads.
_ELASTIC_REQUIRED = ("elasticsearch_url", "kibana_url")
_SIEM_SOURCES = (kibana.SOURCE, wazuh.SOURCE, splunk.SOURCE)
_VOLUME = ("quiet", "noisy", "silent")
_TOO_NEW = (quiet.TOO_NEW, quiet.RULE_TOO_NEW)
_RUN = {"fails_to_run": "fails to run", "data_silent": "data silent"}
_LOUD = {"quiet": "too loud", "noisy": "went noisy", "silent": "went silent"}
_PASS = {
    "quiet": "No rule is over its alerts-per-day limit.",
    "noisy": "No rule went noisy.",
    "silent": "No rule went silent.",
}
_TOOLS = {
    "elastic": "Elastic",
    "splunk": "Splunk",
    "wazuh": "Wazuh",
    "repo": "Repo",
    "overlay": "Vigil",
}


def produce() -> list[dict[str, Any]]:
    """Coverage checks for one report. A failed read is one not-checked check."""
    refreshed = False
    try:
        with _sync_lock():
            _refresh()
            refreshed = True
    except Exception as exc:  # noqa: BLE001 - the stored checks are still read
        logger.warning("Day 1 Report rule-check refresh: %s", type(exc).__name__)
        if not refreshed:
            _refresh()
    checks: list[dict[str, Any]] = []
    checks.extend(_guard("coverage.technique", "technique coverage", _techniques))
    checks.extend(_guard("coverage.blind_spot", "blind spots", _blind_spots))
    checks.extend(_guard("coverage.paper", "covered on paper", _paper))
    checks.extend(_guard("coverage.run", "rules that don't run", _wont_run))
    checks.extend(_volume())
    return checks


@contextmanager
def _sync_lock() -> Iterator[None]:
    """The detection sync's lock, so this write and a sync don't share a checks row."""
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


def _refresh() -> None:
    """Quiet, then Sees, per SIEM. A sweep that fails leaves the stored checks."""
    _call("wazuh quiet", quiet.sweep_wazuh)
    _call("wazuh sees", wazuh_sees.sweep)
    elastic = _config(ELASTIC, _ELASTIC_REQUIRED)
    if elastic is not None:
        _call("elastic quiet", lambda: quiet.sweep_elastic(elastic))
        _call("elastic sees", lambda: sees.sweep_elastic(elastic))
    splunk_config = _config(SPLUNK, SPLUNK_SELFHOSTED_REQUIRED)
    if splunk_config is not None:
        _call("splunk quiet", lambda: quiet.sweep_splunk(splunk_config))
        _call("splunk sees", lambda: splunk_sees.sweep(splunk_config))


def _call(label: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the stored checks still report
        logger.warning("Day 1 Report did not refresh %s: %s", label, type(exc).__name__)


def _config(descriptor: Any, required: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """The integration's config when it is on and complete enough to sweep."""
    try:
        integrations = load_integrations_config()
        if not integrations["configured"]:
            return None
        if descriptor.id not in integrations["enabled_integrations"]:
            return None
        config = resolve(descriptor)
        if missing(config, *required):
            return None
        return config
    except Exception as exc:  # noqa: BLE001 - that SIEM's sweep is skipped
        logger.warning(
            "Day 1 Report could not read %s for rule checks: %s",
            descriptor.id,
            type(exc).__name__,
        )
        return None


def _guard(
    check_id: str, label: str, read: Callable[[], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - one not-checked check, report completes
        logger.error("Day 1 Report could not read %s: %s", label, type(exc).__name__)
        return [_item(check_id, NOT_CHECKED, f"Could not read {label}.")]


def _techniques() -> list[dict[str, Any]]:
    rows = [
        row
        for row in technique_coverage()["techniques"]
        if row["state"] in ("unproven", "no_rule")
    ]
    if not rows:
        return [
            _item(
                "coverage.technique",
                PASS,
                "No technique is unproven or without an active rule.",
            )
        ]
    return [
        _item(
            f"coverage.technique.{row['technique']}",
            FAIL,
            _technique_reason(row),
        )
        for row in rows
    ]


def _technique_reason(row: dict[str, Any]) -> str:
    title = f"{row['technique']} {row.get('name') or ''}".strip()
    if row["state"] == "no_rule":
        seen = int(row.get("seen") or 0)
        extra = f" Seen on {seen} alert{'' if seen == 1 else 's'}." if seen else ""
        return f"{title}: no active rule.{extra}"
    rules = int(row.get("rules") or 0)
    noun = "rule" if rules == 1 else "rules"
    verb = "declares" if rules == 1 else "declare"
    return f"{title}: rule, not proven. {rules} active {noun} {verb} it."


def _blind_spots() -> list[dict[str, Any]]:
    spots = catalog.blind_spots()
    if not spots:
        return [_item("coverage.blind_spot", PASS, "No data source is a blind spot.")]
    return [
        _item(
            "coverage.blind_spot." + _suffix(spot.get("data_source")),
            FAIL,
            _spot_reason(spot),
        )
        for spot in spots
    ]


def _spot_reason(spot: dict[str, Any]) -> str:
    source = spot.get("data_source") or "unknown"
    rules = list(spot.get("rules") or [])
    names = []
    for rule in rules[:5]:
        title = (rule.get("title") or "").strip() or f"rule {rule.get('rule_id')}"
        names.append(f"{title} ({_tool(rule.get('tool') or '')})")
    listed = ", ".join(names)
    extra = len(rules) - len(names)
    if extra:
        listed = f"{listed} +{extra}"
    count = len(rules)
    noun = "rule" if count == 1 else "rules"
    if listed:
        return f"{source}: blind spot. {count} {noun} fail Sees on it: {listed}."
    return f"{source}: blind spot."


def _paper() -> list[dict[str, Any]]:
    """Covered on paper, built the way identify_gaps builds the gap report."""
    report = gap_report(
        "",
        read_alert_labels(""),
        catalog.rules_by_technique(),
        catalog.rules_by_tactic(),
        catalog.rule_counts(),
        catalog.siem_checks_by_technique(),
        catalog.blind_spots(),
    )
    techniques = list(report.get("covered_on_paper_only") or [])
    if not techniques:
        return [
            _item(
                "coverage.paper",
                PASS,
                "No technique is covered on paper only.",
            )
        ]
    return [
        _item(
            f"coverage.paper.{technique}",
            FAIL,
            f"{technique}: covered on paper only. Every deployed SIEM rule "
            "on it fails Runs or Sees.",
        )
        for technique in techniques
    ]


def _wont_run() -> list[dict[str, Any]]:
    rows = _health_rules("fails_to_run") + _health_rules("data_silent")
    if not rows:
        return [
            _item(
                "coverage.run",
                PASS,
                "No rule fails to run, and none is data silent.",
            )
        ]
    checks = []
    for row in rows:
        name = (row.get("name") or "").strip() or f"rule {row.get('rule_id')}"
        kind = _RUN.get(row.get("health") or "", "does not run")
        why = (row.get("why") or "").strip()
        reason = f"{name} ({_tool(row.get('tool') or '')}): {kind}."
        if why:
            reason = f"{reason} {why if why.endswith('.') else why + '.'}"
        checks.append(_item(f"coverage.run.{row['rule_id']}", FAIL, reason))
    return checks


def _health_rules(state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = catalog.health_listing(health=state, offset=offset)
        batch = list(page["rules"])
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= page["matched"]:
            break
    return rows


def _volume() -> list[dict[str, Any]]:
    try:
        rules = _active_siem_rules()
    except Exception as exc:  # noqa: BLE001 - each category is not checked
        logger.error(
            "Day 1 Report could not read Quiet, Noisy and Silent: %s",
            type(exc).__name__,
        )
        return [
            _item(
                f"coverage.{key}",
                NOT_CHECKED,
                "Could not read Quiet, Noisy and Silent.",
            )
            for key in _VOLUME
        ]
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in _VOLUME}
    for rule_id, name, source, checks in rules:
        stored = checks or {}
        label = f"{(name or '').strip() or f'rule {rule_id}'} ({_tool(source)})"
        for key in _VOLUME:
            gap = _volume_gap(f"coverage.{key}.{rule_id}", label, stored.get(key), key)
            if gap is not None:
                grouped[key].append(gap)
    out: list[dict[str, Any]] = []
    for key in _VOLUME:
        out.extend(grouped[key] or [_item(f"coverage.{key}", PASS, _PASS[key])])
    return out


def _active_siem_rules() -> list[tuple[Any, Any, Any, Any]]:
    with get_db_manager().session_scope() as session:
        return session.execute(
            select(
                DetectionRule.id,
                DetectionRule.name,
                DetectionRule.source,
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
                DetectionRule.status == "active",
                DetectionRule.source.in_(_SIEM_SOURCES),
            )
            .order_by(DetectionRule.id)
        ).all()


def _volume_gap(
    check_id: str, label: str, stored: Any, key: str
) -> Optional[dict[str, Any]]:
    """A rule's stored Quiet, Noisy or Silent result, or nothing when it passes.

    A missing result was not written: the sweep stopped on the search budget.
    Too-new judgements stay not checked. Anything else keeps its stored result,
    with checked_at as its age.
    """
    if not isinstance(stored, dict) or not stored.get("result"):
        return _item(check_id, NOT_CHECKED, f"{label}: not checked: search budget.")
    reason = str(stored.get("reason") or "")
    checked = stored.get("checked_at")
    if reason in _TOO_NEW:
        return _item(check_id, NOT_CHECKED, f"{label}: not checked: too new.", checked)
    if stored.get("result") == "pass":
        return None
    text = _kept(reason) or f"{stored.get('result')}."
    if stored.get("result") == "fail":
        return _item(check_id, FAIL, f"{label}: {_LOUD[key]}. {text}", checked)
    return _item(check_id, NOT_CHECKED, f"{label}: {text}", checked)


def _kept(reason: str) -> str:
    text = reason.strip()
    if text.lower().startswith("fail: "):
        text = text[6:].strip()
    if text and not text.endswith("."):
        text += "."
    return text


def _item(
    check_id: str,
    outcome: str,
    reason: str,
    checked_at: Any = None,
) -> dict[str, Any]:
    spec = spec_for(check_id)
    evidence: dict[str, Any] = {}
    stamp = _stamp(checked_at)
    if stamp:
        evidence["checked_at"] = stamp
    return {
        "id": check_id,
        "module": spec.module,
        "outcome": outcome,
        "reason": reason,
        "evidence": evidence,
    }


def _stamp(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _tool(tool: str) -> str:
    return _TOOLS.get(tool, tool or "unknown")


def _suffix(value: Any) -> str:
    text = str(value or "").strip() or "unknown"
    return " ".join(text.split())


def by_layer() -> dict[str, Any]:
    """The by-layer read (scope all) as stored figures, without its source line.

    Read after :func:`produce`, so it sees the rule checks that run refreshed.
    It applies no demo filter; neither does the rest of this module.
    """
    from core.detections.inventory.coverage import by_layer as read_by_layer

    body = asyncio.run(read_by_layer(technique_coverage(), "all"))
    body.pop("source", None)
    return body
