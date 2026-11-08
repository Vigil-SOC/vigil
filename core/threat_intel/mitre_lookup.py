"""Shared MITRE ATT&CK technique → name/tactic resolution.

Single source of truth used by:
- services/api/routers/analytics.py — get_mitre_technique_distribution
- the ATT&CK agent tool (get_technique_rollup)

Findings carry MITRE data in `mitre_predictions: dict | list` (multiple
formats — see iter_techniques below for the dispatching). A confidence that is
not a score (null, a bool, a string) means the source named the technique and
gave no confidence. ``is_score``, ``meets_threshold``, ``rank_by_confidence``
and ``top_technique`` are defined in ``core.storage.technique_confidence`` and
re-exported here, so every reader shares that order and storage can rank
without importing this module.
"""

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from core.storage import technique_confidence
from core.time import utcnow

is_score = technique_confidence.is_score
meets_threshold = technique_confidence.meets_threshold
rank_by_confidence = technique_confidence.rank_by_confidence
top_technique = technique_confidence.top_technique


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


ATTACK_TECHNIQUES_FILE = Path(__file__).with_name("attack_techniques.json")


@lru_cache(maxsize=1)
def _attack_data() -> dict:
    with open(ATTACK_TECHNIQUES_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def attack_techniques_version() -> str:
    """The ATT&CK release attack_techniques.json was generated from."""
    return _attack_data()["version"]


def attack_techniques() -> dict[str, dict]:
    """Every Enterprise ATT&CK technique and sub-technique id in the pinned release
    (attack_techniques.json, written by scripts/generate_attack_techniques.py), as
    {id: {"name", "tactics", "status", "replaced_by"?}}. Status is active, revoked
    or deprecated."""
    return _attack_data()["techniques"]


def _tactic_title(phase: str) -> str:
    """ATT&CK's kill-chain phase name as a label: command-and-control -> Command and Control."""
    return " ".join(w if w == "and" else w.capitalize() for w in phase.split("-") if w)


def _name_and_tactic(technique_id: str) -> tuple[Optional[str], Optional[str]]:
    entry = attack_techniques().get(technique_id.upper())
    if not entry:
        return None, None
    tactics = entry.get("tactics") or []
    name = entry["name"]
    if entry["status"] == "revoked":
        name += (
            f" (revoked: use {entry['replaced_by']})"
            if entry.get("replaced_by")
            else " (revoked)"
        )
    return name, _tactic_title(tactics[0]) if tactics else None


def resolve_technique(
    tech: dict | str,
    technique_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """Resolve a technique reference to (technique_id, technique_name, tactic).

    Resolution order:
      1. Explicit `technique_name`/`name` and `tactic`/`tactics[0]` on the dict.
      2. Full ID lookup in attack_techniques.json.
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

    fallback_name, fallback_tactic = _name_and_tactic(tid)
    if fallback_name is None:
        fallback_name, fallback_tactic = _name_and_tactic(tid.split(".")[0])

    name = explicit_name or fallback_name or tid
    tactic = explicit_tactic or fallback_tactic or "Unknown"
    return (tid, name, tactic)


def iter_techniques(finding: dict) -> Iterable[dict]:
    """Yield per-technique dicts from a finding regardless of storage shape.

    Each yielded dict has at least `technique_id` and may have `confidence`,
    `technique_name`, `tactic`. Callers should pipe results through
    `resolve_technique` to fill in name/tactic.

    Handles:
      - `mitre_predictions: dict[tech_id, confidence]`. A value that is not a
        score yields the technique with `confidence: None`.
      - `mitre_predictions: {"techniques": [...]}` (a map value is a list or dict)
      - `mitre_predictions: {technique_id | id, confidence, ...}`, one technique
      - `mitre_predictions: list[dict]`
    """
    predictions = finding.get("mitre_predictions")
    if not predictions:
        return

    if isinstance(predictions, dict):
        # A single technique dict (its own id key) is not a technique map.
        single = "technique_id" in predictions or "id" in predictions
        if not single and not any(
            isinstance(v, (list, dict)) for v in predictions.values()
        ):
            for tech_id, confidence in predictions.items():
                yield {
                    "technique_id": tech_id,
                    "confidence": confidence if is_score(confidence) else None,
                }
            return
        nested = predictions.get("techniques") or [predictions]
        for tech in nested:
            if isinstance(tech, dict) and (tech.get("technique_id") or tech.get("id")):
                yield tech
        return

    if isinstance(predictions, list):
        for tech in predictions:
            if isinstance(tech, dict) and (tech.get("technique_id") or tech.get("id")):
                yield tech
