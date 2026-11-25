"""Read every analytics rule in the Sentinel workspace, and whether it is on.

Credentials are the ``azure-sentinel`` integration's, built as its connector
builds them. Only ``alert_rules.list`` is called: nothing is written to Azure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, List, Mapping, Optional

from core.detections.inventory.readers import _json_safe, _tactics, _techniques
from core.detections.inventory.record import RuleRecord

# The string a Sentinel finding's data_source carries, so the two halves meet.
SOURCE = "azure_sentinel"


def _value(value: Any) -> Any:
    # An SDK enum reads as its wire value.
    return getattr(value, "value", value)


def iso_duration(value: Any) -> Optional[str]:
    """A timedelta (or an ISO string already) as ISO-8601: PT1H, PT0S, P1D."""
    if value is None or value == "":
        return None
    if not isinstance(value, timedelta):
        return str(value)
    seconds = int(value.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    time = "".join(
        f"{n}{u}" for n, u in ((hours, "H"), (minutes, "M"), (seconds, "S")) if n
    )
    if not days and not time:
        return "PT0S"
    return (f"P{days}D" if days else "P") + (f"T{time}" if time else "")


def record_of(rule: Any) -> RuleRecord:
    """One SDK alert rule (ScheduledAlertRule, NrtAlertRule, FusionAlertRule, ...)."""
    override = getattr(rule, "alert_details_override", None)
    return RuleRecord(
        native_key=str(rule.name),  # the segment an incident's rule ids end in
        name=str(getattr(rule, "display_name", None) or rule.name),
        language="kql",
        path=None,
        query=str(getattr(rule, "query", None) or ""),
        techniques=_techniques(getattr(rule, "techniques", None)),
        tactics=_tactics([_value(t) for t in getattr(rule, "tactics", None) or []]),
        logic={
            "frequency": iso_duration(getattr(rule, "query_frequency", None)),
            "lookback": iso_duration(getattr(rule, "query_period", None)),
            "kind": _value(getattr(rule, "kind", None)) or type(rule).__name__,
            "severity": _value(getattr(rule, "severity", None)),
            "alert_title": getattr(override, "alert_display_name_format", None),
        },
        content=_json_safe(rule.as_dict()),
        status="disabled" if getattr(rule, "enabled", None) is False else "active",
    )


def read_analytics_rules(config: Mapping[str, Any]) -> List[RuleRecord]:
    """Every analytics rule the workspace lists, or raise: a short list must not
    reach sync. Raises ImportError when the Azure SDK is not installed."""
    from azure.identity import ClientSecretCredential
    from azure.mgmt.securityinsight import SecurityInsights

    credential = ClientSecretCredential(
        tenant_id=config["tenant_id"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
    )
    client = None
    try:
        client = SecurityInsights(credential, config["subscription_id"])
        rules = client.alert_rules.list(
            resource_group_name=config["resource_group"],
            workspace_name=config["workspace_name"],
        )
        return [record_of(rule) for rule in rules]
    finally:
        if client is not None:
            client.close()
        credential.close()
