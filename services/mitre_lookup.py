"""Shared MITRE ATT&CK technique → name/tactic resolution.

Single source of truth used by:
- backend/api/attack.py — /attack/techniques/rollup
- backend/api/analytics.py — get_mitre_technique_distribution
- services/attack_data_loader.py — visualization data

Findings in this codebase carry MITRE data in two shapes:
- Demo path: `predicted_techniques: list[{technique_id, confidence, technique_name}]`
- Production path: `mitre_predictions: dict | list` (multiple formats — see
  iter_techniques below for the dispatching).
"""

from typing import Iterable, Optional

# {technique_id: (name, tactic)} — extend as ATT&CK coverage grows.
TECHNIQUE_NAME_FALLBACKS: dict[str, tuple[str, str]] = {
    "T1071.001": ("Web Protocols", "Command and Control"),
    "T1071.004": ("DNS", "Command and Control"),
    "T1573.001": ("Encrypted Channel", "Command and Control"),
    "T1021.001": ("RDP", "Lateral Movement"),
    "T1021.002": ("SMB/Windows Admin Shares", "Lateral Movement"),
    "T1048.003": ("Exfiltration Over DNS", "Exfiltration"),
    "T1190": ("Exploit Public-Facing Application", "Initial Access"),
    "T1078": ("Valid Accounts", "Initial Access"),
    "T1059.001": ("PowerShell", "Execution"),
    "T1018": ("Remote System Discovery", "Discovery"),
    # Base techniques referenced by the legacy tactics-summary mapping.
    "T1071": ("Application Layer Protocol", "Command and Control"),
    "T1573": ("Encrypted Channel", "Command and Control"),
    "T1059": ("Command and Scripting Interpreter", "Execution"),
    "T1055": ("Process Injection", "Defense Evasion"),
    "T1036": ("Masquerading", "Defense Evasion"),
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
