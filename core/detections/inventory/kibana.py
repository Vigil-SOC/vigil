"""Read every rule in the Kibana detection engine, and whether each one runs.

Credentials are the ``elastic-siem`` integration's, as its credential check
uses them. Only ``_find`` is called: nothing is written to Kibana, and the Runs
result comes from the same page as the rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from core.detections.inventory.readers import ELASTIC_LOGIC_KEYS, _tactics, _techniques
from core.detections.inventory.record import RuleRecord

SOURCE = "elastic"

# Kibana's last_execution.status values that are not a result yet.
_IN_FLIGHT = ("running", "going to run")


def _when(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def runs_of(rule: Mapping[str, Any]) -> Optional[Dict[str, Optional[str]]]:
    """``{result, reason}`` from the rule's last execution, or None for no new word.

    A run from before the rule's last change in Kibana ran other logic, so it
    says nothing about this version.
    """
    if not rule.get("enabled"):
        return None
    last = (rule.get("execution_summary") or {}).get("last_execution")
    if not last:
        return {"result": "unknown", "reason": "Kibana reports no run of this rule yet"}
    status = str(last.get("status") or "")
    if status in _IN_FLIGHT:
        return None
    ran, changed = _when(last.get("date")), _when(rule.get("updated_at"))
    if ran and changed and ran < changed:
        return None
    if status == "succeeded":
        return {"result": "pass", "reason": None}
    if status in ("failed", "partial failure"):
        message = str(last.get("message") or "").strip()
        return {
            "result": "fail",
            "reason": f"{status}: {message}" if message else status,
        }
    return {"result": "unknown", "reason": f"Kibana reports last run status {status!r}"}


def _threat(rule: Mapping[str, Any]) -> tuple[List[str], List[str]]:
    techniques: List[str] = []
    tactics: List[str] = []
    for entry in rule.get("threat") or []:
        if not isinstance(entry, dict):
            continue
        tactic = entry.get("tactic") or {}
        if isinstance(tactic.get("name"), str):
            tactics.append(tactic["name"])
        for technique in entry.get("technique") or []:
            techniques.append(technique.get("id"))
            techniques.extend(s.get("id") for s in technique.get("subtechnique") or [])
    return _techniques([t for t in techniques if t]), _tactics(tactics)


def record_of(rule: Mapping[str, Any]) -> RuleRecord:
    """The rule as a repo toml of it would read (readers._elastic), plus Runs."""
    techniques, tactics = _threat(rule)
    return RuleRecord(
        native_key=str(rule["rule_id"]),
        name=str(rule.get("name") or rule["rule_id"]),
        language="elastic",
        path=None,
        query=str(rule.get("query") or ""),
        techniques=techniques,
        tactics=tactics,
        logic={k: rule.get(k) for k in ELASTIC_LOGIC_KEYS},
        content={k: v for k, v in rule.items() if k != "execution_summary"},
        status="active" if rule.get("enabled") else "disabled",
        runs=runs_of(rule),
    )


def service_of(config: Mapping[str, Any]) -> Any:
    """An ElasticService on the elastic-siem integration's resolved config."""
    from core.integrations.elastic.client import ElasticService

    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    return ElasticService(
        elasticsearch_url=config["elasticsearch_url"],
        kibana_url=config.get("kibana_url"),
        api_key=config.get("api_key"),
        username=config.get("username"),
        password=config.get("password"),
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )


def read_kibana(config: Mapping[str, Any]) -> List[RuleRecord]:
    """Every rule Kibana lists, or raise: a short list must not reach sync."""
    service = service_of(config)

    async def _list() -> List[Dict[str, Any]]:
        try:
            return await service.list_detection_rules()
        finally:
            await service.close()

    return [record_of(rule) for rule in asyncio.run(_list())]
