"""Read every rule the Wazuh manager has loaded, and whether each one runs.

Credentials are the ``wazuh`` integration's. Only ``GET /rules`` is called:
nothing is written to the manager. A rule the manager loaded runs on every
event it decodes, so its Runs is ``pass``; a rule excluded in ossec.conf is
``disabled`` and has none.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping

from core.detections.inventory.readers import _techniques
from core.detections.inventory.record import RuleRecord

SOURCE = "wazuh"

# Where local rules live; an overwrite="yes" rule there replaces the ruleset copy.
_LOCAL_DIR = "etc/"


def record_of(rule: Mapping[str, Any]) -> RuleRecord:
    """The rule as the manager lists it. Its mitre is technique ids only, no tactics."""
    key = str(rule["id"])
    enabled = rule.get("status") != "disabled"
    dirname, filename = rule.get("relative_dirname"), rule.get("filename")
    return RuleRecord(
        native_key=key,
        name=str(rule.get("description") or key),
        language="wazuh",
        path=f"{dirname}/{filename}" if dirname and filename else filename,
        techniques=_techniques(rule.get("mitre") or []),
        logic={"details": rule.get("details"), "level": rule.get("level")},
        content=dict(rule),
        status="active" if enabled else "disabled",
        runs={"result": "pass", "reason": "loaded by the manager"} if enabled else None,
    )


def _local(rule: Mapping[str, Any]) -> bool:
    return str(rule.get("relative_dirname") or "").startswith(_LOCAL_DIR)


def effective(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per id: the manager lists an overwritten rule twice; the local copy wins.

    Any other repeat (two ruleset copies, two local ones) raises, as Kibana's does.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        key = str(rule["id"])
        held = by_id.get(key)
        if held is None:
            by_id[key] = rule
        elif _local(rule) != _local(held):
            by_id[key] = rule if _local(rule) else held
        else:
            raise ValueError(f"Wazuh lists rule {key} twice")
    return list(by_id.values())


def read_wazuh(config: Mapping[str, Any]) -> List[RuleRecord]:
    """Every rule the manager lists, or raise: a short list must not reach sync."""
    from core.integrations.wazuh.client import WazuhService

    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = WazuhService(
        api_url=config["api_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )

    async def _list() -> List[Dict[str, Any]]:
        try:
            rules, total = await service.list_rules()
        finally:
            await service.close()
        if len(rules) < total:
            raise ValueError(f"Wazuh listed {len(rules)} of {total} rules")
        return rules

    return [record_of(rule) for rule in effective(asyncio.run(_list()))]
