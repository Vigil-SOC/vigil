"""Shared MITRE ATT&CK technique → name/tactic resolution.

Single source of truth used by:
- services/api/routers/analytics.py — get_mitre_technique_distribution
- the ATT&CK agent tool (get_technique_rollup)

Findings in this codebase carry MITRE data in two shapes:
- Demo path: `predicted_techniques: list[{technique_id, confidence, technique_name}]`
- Production path: `mitre_predictions: dict | list` (multiple formats — see
  iter_techniques below for the dispatching).
"""

from datetime import datetime, timedelta
from typing import Iterable, Optional

from core.time import utcnow


def get_time_range(time_range: str) -> tuple[datetime, datetime]:
    """Get start and end datetime for the given time range.

    Lives here (not in services.api.routers.analytics) so that `core/` domains
    and the API routers can both depend on it without either importing the
    other.
    """
    end_time = utcnow()

    if time_range == "24h":
        start_time = end_time - timedelta(hours=24)
    elif time_range == "7d":
        start_time = end_time - timedelta(days=7)
    elif time_range == "30d":
        start_time = end_time - timedelta(days=30)
    elif time_range == "all":
        start_time = datetime(2000, 1, 1)
    else:
        start_time = end_time - timedelta(days=7)  # Default to 7 days

    return start_time, end_time


# {technique_id: (name, tactic)} — extend as ATT&CK coverage grows.
TECHNIQUE_NAME_FALLBACKS: dict[str, tuple[str, str]] = {
    "T1003": ("OS Credential Dumping", "Credential Access"),
    "T1003.001": ("LSASS Memory", "Credential Access"),
    "T1003.002": ("Security Account Manager", "Credential Access"),
    "T1018": ("Remote System Discovery", "Discovery"),
    "T1021": ("Remote Services", "Lateral Movement"),
    "T1021.001": ("Remote Desktop Protocol", "Lateral Movement"),
    "T1021.002": ("SMB/Windows Admin Shares", "Lateral Movement"),
    "T1027": ("Obfuscated Files or Information", "Defense Evasion"),
    "T1036": ("Masquerading", "Defense Evasion"),
    "T1041": ("Exfiltration Over C2 Channel", "Exfiltration"),
    "T1047": ("Windows Management Instrumentation", "Execution"),
    "T1048.003": ("Exfiltration Over DNS", "Exfiltration"),
    "T1053": ("Scheduled Task/Job", "Execution"),
    "T1053.005": ("Scheduled Task", "Execution"),
    "T1055": ("Process Injection", "Defense Evasion"),
    "T1055.001": ("DLL Injection", "Defense Evasion"),
    "T1055.012": ("Process Hollowing", "Defense Evasion"),
    "T1059": ("Command and Scripting Interpreter", "Execution"),
    "T1059.001": ("PowerShell", "Execution"),
    "T1059.003": ("Windows Command Shell", "Execution"),
    "T1070": ("Indicator Removal", "Defense Evasion"),
    "T1070.004": ("File Deletion", "Defense Evasion"),
    "T1071": ("Application Layer Protocol", "Command and Control"),
    "T1071.001": ("Web Protocols", "Command and Control"),
    "T1071.004": ("DNS", "Command and Control"),
    "T1078": ("Valid Accounts", "Initial Access"),
    "T1078.001": ("Default Accounts", "Initial Access"),
    "T1078.002": ("Domain Accounts", "Initial Access"),
    "T1110": ("Brute Force", "Credential Access"),
    "T1110.003": ("Password Spraying", "Credential Access"),
    "T1190": ("Exploit Public-Facing Application", "Initial Access"),
    "T1486": ("Data Encrypted for Impact", "Impact"),
    "T1490": ("Inhibit System Recovery", "Impact"),
    "T1547": ("Boot or Logon Autostart Execution", "Persistence"),
    "T1547.001": ("Registry Run Keys / Startup Folder", "Persistence"),
    "T1566": ("Phishing", "Initial Access"),
    "T1566.001": ("Spearphishing Attachment", "Initial Access"),
    "T1566.002": ("Spearphishing Link", "Initial Access"),
    "T1567": ("Exfiltration Over Web Service", "Exfiltration"),
    "T1567.002": ("Exfiltration to Cloud Storage", "Exfiltration"),
    "T1573": ("Encrypted Channel", "Command and Control"),
    "T1573.001": ("Encrypted Channel", "Command and Control"),
    "T1574": ("Hijack Execution Flow", "Persistence"),
    "T1574.001": ("DLL Search-Order Hijacking", "Persistence"),
}


def resolve_technique(
    tech: dict | str,
    technique_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """Resolve a technique reference to (technique_id, technique_name, tactic).

    Resolution order:
      1. Explicit `technique_name`/`name` and `tactic`/`tactics[0]` on the dict.
      2. Full ID lookup in TECHNIQUE_NAME_FALLBACKS.
      3. Base ID (strip `.NNN` sub-technique) lookup.
      4. Fallback (technique_id, technique_id, "Unknown").
    """
    if isinstance(tech, dict):
        tid = tech.get("technique_id") or tech.get("id") or technique_id or ""
        explicit_name = tech.get("technique_name") or tech.get("name")
        tactics_val = tech.get("tactics")
        explicit_tactic = tech.get("tactic") or (
            tactics_val[0] if isinstance(tactics_val, list) and tactics_val else None
        )
    else:
        tid = tech or technique_id or ""
        explicit_name = None
        explicit_tactic = None

    if not tid:
        return ("", "", "Unknown")

    fallback_name, fallback_tactic = TECHNIQUE_NAME_FALLBACKS.get(tid, (None, None))
    if fallback_name is None:
        base_id = tid.split(".")[0]
        fallback_name, fallback_tactic = TECHNIQUE_NAME_FALLBACKS.get(
            base_id, (None, None)
        )

    name = explicit_name or fallback_name or tid
    tactic = explicit_tactic or fallback_tactic or "Unknown"
    return (tid, name, tactic)


def iter_techniques(finding: dict) -> Iterable[dict]:
    """Yield per-technique dicts from a finding regardless of storage shape.

    Each yielded dict has at least `technique_id` and may have `confidence`,
    `technique_name`, `tactic`. Callers should pipe results through
    `resolve_technique` to fill in name/tactic.

    Handles:
      - Demo shape: `predicted_techniques: list[{technique_id, confidence, ...}]`
      - Production `mitre_predictions: dict[tech_id, confidence]`
      - Production `mitre_predictions: {"techniques": [...]}` /
        `{"predicted_techniques": [...]}`
      - Production `mitre_predictions: list[dict]`
    """
    pt = finding.get("predicted_techniques")
    if isinstance(pt, list) and pt:
        for tech in pt:
            if isinstance(tech, dict) and tech.get("technique_id"):
                yield tech
        return

    predictions = finding.get("mitre_predictions")
    if not predictions:
        return

    if isinstance(predictions, dict):
        if predictions and all(
            isinstance(v, (int, float)) for v in predictions.values()
        ):
            for tech_id, confidence in predictions.items():
                yield {"technique_id": tech_id, "confidence": confidence}
            return
        nested = (
            predictions.get("techniques")
            or predictions.get("predicted_techniques")
            or [predictions]
        )
        for tech in nested:
            if isinstance(tech, dict) and (tech.get("technique_id") or tech.get("id")):
                yield tech
        return

    if isinstance(predictions, list):
        for tech in predictions:
            if isinstance(tech, dict) and (tech.get("technique_id") or tech.get("id")):
                yield tech
