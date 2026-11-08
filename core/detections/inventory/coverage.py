"""ATT&CK coverage by technique and SIEM: the heat map (#4103).

One row per technique an active rule declares or a stored alert shows, in
ATT&CK tactic columns, with its state:

- ``proven``: an active SIEM copy of a rule declaring it passes both Runs and
  Sees, as ``catalog.siem_checks_by_technique`` counts ``proven`` (#3910);
- ``unproven``: an active rule declares it, but no SIEM copy of one is proven;
- ``no_rule``: no active rule declares it: only a disabled rule does, or only
  alerts show it.

Each row is split by SIEM (Elastic, Splunk, Wazuh): that SIEM's active copies
declaring it and how many are proven. A rule counts once however many copies
it has (``catalog._rule_of_copy``, as the Rules page counts rules). A disabled
rule is no coverage, but each technique says how many disabled rules declare it,
so the map lists every technique the Rules page counts as declared (``mapped``).

Ported from Alex Margaris's heat map on the 1.x line (vigil-1.x d0a56ef6 and
7ee99b3f, ``core/detections/engineering/program.py::coverage``), without its
threat model and proposal replays, which main does not have: "proven" here is
main's Runs-and-Sees meaning, and a passing Sigma replay (``checks.replay``) is
a detail on the row. Read-only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select

from core.detections.inventory import catalog
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion
from core.storage.models.finding import FindingMitrePrediction
from core.threat_intel.mitre_lookup import attack_tactics, resolve_technique

SIEMS = ("elastic", "splunk", "wazuh")
UNMAPPED_TACTIC = "Unmapped"
# Defense Evasion, which ATT&CK 18 split into Stealth and Defense Impairment, is
# kept as a column for a rule that still declares it on an id ATT&CK lacks.
LEGACY_TACTICS = {"Stealth": "Defense Evasion"}
TECHNIQUE_ID = re.compile(r"^T\d{4}(\.\d{3})?$")
STATES = ("proven", "unproven", "no_rule")


def _copies(session: Any) -> List[Any]:
    """Each listed (active or disabled) copy's techniques, tactics, source and
    latest Runs, Sees and replay, as the Rules page lists copies."""
    return session.execute(
        select(
            DetectionRule.id,
            DetectionRule.status,
            DetectionRule.source,
            DetectionRule.language,
            DetectionRule.techniques,
            DetectionRule.tactics,
            catalog.RUNS.label("runs"),
            catalog.SEES.label("sees"),
            DetectionRuleVersion.checks["replay"]["result"].astext.label("replay"),
        )
        .join(DetectionRuleVersion, catalog._latest_join())
        .where(catalog.LISTED)
    ).all()


def _seen(session: Any) -> Dict[str, int]:
    """How many stored alerts were mapped to each technique."""
    seen: Dict[str, int] = {}
    column = FindingMitrePrediction.technique_id
    rows = session.execute(
        select(column, func.count()).where(column.is_not(None)).group_by(column)
    )
    for label, count in rows:
        value = str(label).strip().upper()
        if TECHNIQUE_ID.match(value):
            seen[value] = seen.get(value, 0) + count
    return seen


def tactic_order() -> List[str]:
    """The tactic columns: the pinned ATT&CK release's own, in matrix order, so a
    tactic ATT&CK adds can't fall into Unmapped, with Defense Evasion before
    Stealth; then Unmapped."""
    order: List[str] = []
    for tactic in attack_tactics():
        if tactic in LEGACY_TACTICS:
            order.append(LEGACY_TACTICS[tactic])
        order.append(tactic)
    return [*dict.fromkeys(order), UNMAPPED_TACTIC]


def _label(tactic: str) -> str:
    """defense_evasion, Defense-Evasion, defense evasion -> defense evasion."""
    return re.sub(r"[\s_-]+", " ", tactic.strip().lower())


def _tactic_of(
    technique: str, declared: Dict[str, str], columns: Dict[str, str]
) -> Tuple[str, str]:
    """(name, tactic column): ATT&CK's own, else the one tactic a rule declares."""
    _, name, tactic = resolve_technique(technique)
    if tactic == "Unknown":
        tactic = declared.get(technique, UNMAPPED_TACTIC)
    return name, columns.get(_label(tactic), UNMAPPED_TACTIC)


def _siem_of(source: str) -> Optional[str]:
    """The SIEM a copy runs in; None for a repo or overlay rule."""
    return None if source.startswith(catalog.REPO_PREFIX) else source


def coverage() -> Dict[str, Any]:
    """The heat map: each technique's state and SIEM split, the tactics in
    order, and the totals."""
    with get_db_manager().session_scope() as session:
        copies = _copies(session)
        seen = _seen(session)
        rule_of = catalog._rule_of_copy(session)
    rules: Dict[str, Set[Any]] = {}
    disabled: Dict[str, Set[Any]] = {}
    replayed: Dict[str, Set[Any]] = {}
    siems: Dict[str, Dict[str, Dict[str, int]]] = {}
    declared: Dict[str, str] = {}
    for copy in copies:
        rule = rule_of.get(copy.id, (copy.id,))
        siem = _siem_of(copy.source)
        for technique in copy.techniques or []:
            if copy.tactics and len(copy.tactics) == 1:
                declared.setdefault(technique, copy.tactics[0])
            if copy.status != "active":
                disabled.setdefault(technique, set()).add(rule)
                continue
            rules.setdefault(technique, set()).add(rule)
            if copy.language == "sigma" and copy.replay == "pass":
                replayed.setdefault(technique, set()).add(rule)
            if siem is None:
                continue
            tally = siems.setdefault(technique, {}).setdefault(
                siem, {"copies": 0, "proven": 0, "failing": 0}
            )
            tally["copies"] += 1
            if copy.runs == copy.sees == "pass":
                tally["proven"] += 1
            if "fail" in (copy.runs, copy.sees):
                tally["failing"] += 1
    order = tactic_order()
    columns = {_label(tactic): tactic for tactic in order}
    rows = []
    for technique in sorted({*rules, *disabled, *seen}):
        split = siems.get(technique, {})
        proven = sum(t["proven"] for t in split.values())
        state = "proven" if proven else "unproven" if technique in rules else "no_rule"
        name, tactic = _tactic_of(technique, declared, columns)
        rows.append(
            {
                "technique": technique,
                "name": name,
                "tactic": tactic,
                "state": state,
                "rules": len(rules.get(technique, ())),
                # Disabled rules declaring it that no active copy of covers.
                "disabled": len(
                    disabled.get(technique, set()) - rules.get(technique, set())
                ),
                "seen": seen.get(technique, 0),
                "replayed": len(replayed.get(technique, ())),
                "siems": {
                    siem: {
                        **split[siem],
                        "state": "proven" if split[siem]["proven"] else "unproven",
                    }
                    for siem in SIEMS
                    if siem in split
                },
            }
        )
    tactics = [t for t in order if any(row["tactic"] == t for row in rows)]
    return {
        "techniques": rows,
        "tactics": tactics,
        "summary": {s: sum(1 for row in rows if row["state"] == s) for s in STATES},
        "rule_total": len(
            {rule_of.get(c.id, (c.id,)) for c in copies if c.status == "active"}
        ),
        # Rules (active or disabled) declaring a technique: the Rules page's ``mapped``.
        "mapped": len({rule_of.get(c.id, (c.id,)) for c in copies if c.techniques}),
        "source": (
            "Active and disabled rules in the inventory (detection_rules, their "
            "latest version's checks) and alerts mapped to a technique "
            "(finding_mitre_predictions). Proven: an active SIEM copy passes Runs "
            "and Sees. A disabled rule is no coverage. A rule counts once however "
            "many copies it has."
        ),
    }
