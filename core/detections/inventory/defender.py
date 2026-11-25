"""Read every Defender XDR custom detection rule from Graph v1.0.

Credentials are the ``microsoft-defender`` integration's. Only
``GET /v1.0/security/rules/detectionRules`` is called: nothing is written to
Defender. The app needs ``CustomDetection.Read.All``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

import httpx

from core.detections.inventory.readers import _json_safe, _tactics, _techniques
from core.detections.inventory.record import RuleRecord
from core.integrations._base.aad_token import fetch_client_credentials_token

# The string a Defender finding's data_source carries, so the two halves meet.
SOURCE = "microsoft_defender"
GRAPH = "https://graph.microsoft.com/"
RULES_URL = GRAPH + "v1.0/security/rules/detectionRules"
SCOPE = GRAPH + ".default"
PERMISSION = "CustomDetection.Read.All"


def alert_title(rule: Mapping[str, Any]) -> Optional[str]:
    """The rule's detectionAction.alertTemplate.title, stripped, or None."""
    action = rule.get("detectionAction") or {}
    template = action.get("alertTemplate") if isinstance(action, dict) else None
    title = template.get("title") if isinstance(template, dict) else None
    return (title.strip() or None) if isinstance(title, str) else None


def record_of(rule: Mapping[str, Any]) -> RuleRecord:
    """One Graph v1.0 detectionRule."""
    schedule = rule.get("schedule") or {}
    action = rule.get("detectionAction") or {}
    template = (action.get("alertTemplate") if isinstance(action, dict) else None) or {}
    # The issue's tactics[] shape, and Graph v1.0's documented category/mitreTechniques.
    tactics: List[Any] = [template.get("category")]
    named = template.get("mitreTechniques")
    techniques: List[Any] = list(named) if isinstance(named, list) else []
    for t in template.get("tactics") or []:
        if not isinstance(t, dict):
            continue
        tactics.append(t.get("tactic"))
        for tech in t.get("techniques") or []:
            if isinstance(tech, dict):
                techniques.append(tech.get("technique"))
                subs = tech.get("subTechniques")
                techniques.extend(subs if isinstance(subs, list) else [])
    return RuleRecord(
        native_key=str(rule["id"]),
        name=str(rule.get("displayName") or rule["id"]),
        language="kql",
        path=None,
        query=str((rule.get("queryCondition") or {}).get("queryText") or ""),
        techniques=_techniques(techniques),
        tactics=_tactics(tactics),
        logic={
            "frequency": schedule.get("frequency") or schedule.get("period"),
            "lookback": None,  # v1.0 does not publish it
            "kind": None,
            "severity": template.get("severity"),
            "alert_title": alert_title(rule),
        },
        content=_json_safe(rule),
        status=(
            "disabled"
            if str(rule.get("status") or "").lower() in ("disabled", "autodisabled")
            or rule.get("isEnabled") is False
            else "active"
        ),
    )


def read_detections(config: Mapping[str, Any]) -> List[RuleRecord]:
    """Every custom detection rule Graph lists, or raise: a short list must not
    reach sync."""
    token = fetch_client_credentials_token(
        "Detection rules",
        config["tenant_id"],
        config["client_id"],
        config["client_secret"],
        SCOPE,
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    rules: Dict[str, RuleRecord] = {}
    seen: Set[str] = set()
    url: Optional[str] = RULES_URL
    with httpx.Client(timeout=30, headers=headers) as client:
        while url:
            if url in seen:
                raise RuntimeError(f"Graph repeated nextLink {url}")
            if not url.startswith(GRAPH):
                raise RuntimeError(f"Graph nextLink is off graph.microsoft.com: {url}")
            seen.add(url)
            resp = client.get(url)
            if resp.status_code == 403:
                raise PermissionError(
                    f"Graph returned 403 on detectionRules: the app needs {PERMISSION}"
                    " (Application, admin-consented)"
                )
            if resp.status_code != 200:
                raise RuntimeError(f"Graph returned HTTP {resp.status_code} on {url}")
            body = resp.json()
            page = body.get("value") if isinstance(body, dict) else None
            if not isinstance(page, list):
                raise RuntimeError("Graph detectionRules page has no value list")
            for rule in page:
                record = record_of(rule)
                if record.native_key in rules:
                    raise RuntimeError(f"Graph listed rule {record.native_key} twice")
                rules[record.native_key] = record
            url = body.get("@odata.nextLink")
    return list(rules.values())
