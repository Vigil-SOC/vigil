"""Which ATT&CK labels stored alerts carry, set against the active detection rules.

A gap is a technique seen on a stored alert that no active rule declares; rules are
counted by detection_rules id. Stored labels are not all techniques: ingest keeps
whatever key a source sent, so a row may name a tactic or an unmapped class. Those
cannot prove a gap, and the report says so rather than guessing which technique ran.

A technique whose SIEM rules (read from Kibana) all fail Runs is covered on paper
only: a rule declares it, and nothing that runs does.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, List, Mapping, Optional

from core.storage.connection import get_db_manager
from core.storage.models import Case, Finding, FindingMitrePrediction, case_findings

RULE_SOURCES = ("sigma", "splunk", "elastic", "kql")
# The rule formats that declare a tactic; Splunk and KQL rules name techniques only.
TACTIC_RULE_SOURCES = ("sigma", "elastic")

SIEMS_NOT_READ_NOTE = (
    "Only Elastic (Kibana) rules are read from a SIEM. Splunk saved searches and "
    "Wazuh rules are not read yet, so what those SIEMs run, and whether it runs, "
    "is not part of this report."
)

TECHNIQUE_ID = re.compile(r"^T\d{4}(\.\d{3})?$", re.IGNORECASE)

# Enterprise ATT&CK, plus stealth and defense impairment, which current Sigma rules tag.
TACTICS = frozenset(
    {
        "reconnaissance",
        "resource development",
        "initial access",
        "execution",
        "persistence",
        "privilege escalation",
        "defense evasion",
        "stealth",
        "defense impairment",
        "credential access",
        "discovery",
        "lateral movement",
        "collection",
        "command and control",
        "exfiltration",
        "impact",
    }
)


def normalize_label(label: str) -> str:
    return re.sub(r"[\s_-]+", " ", label.strip().lower())


@dataclass(frozen=True)
class AlertLabels:
    """The alerts in scope and the labels each carries. An unlabelled one is only counted."""

    scope: str
    total: int
    by_alert: Mapping[str, Collection[str]] = field(default_factory=dict)


def read_alert_labels(context: str = "") -> AlertLabels:
    """Scope by ``context``: empty is every alert, a case id is that case, else a label match."""
    with get_db_manager().session_scope() as session:
        is_case = bool(context) and session.get(Case, context) is not None
        rows = session.query(
            FindingMitrePrediction.finding_id, FindingMitrePrediction.technique_id
        )
        total = session.query(Finding.finding_id)
        if is_case:
            in_case = (
                session.query(case_findings.c.finding_id)
                .filter(case_findings.c.case_id == context)
                .subquery()
            )
            rows = rows.filter(FindingMitrePrediction.finding_id.in_(in_case.select()))
            total = total.filter(Finding.finding_id.in_(in_case.select()))
        by_alert: Dict[str, set] = {}
        for finding_id, label in rows.all():
            by_alert.setdefault(finding_id, set()).add(label)
        count = total.count()

    if not context:
        return AlertLabels("every stored alert", count, by_alert)
    if is_case:
        return AlertLabels(f"case {context}", count, by_alert)
    wanted = normalize_label(context)
    matching = {
        finding_id: labels
        for finding_id, labels in by_alert.items()
        if any(wanted in normalize_label(label) for label in labels)
    }
    return AlertLabels(f'alerts labelled "{context}"', len(matching), matching)


def _classify(alerts: AlertLabels) -> tuple[Counter, Counter, Dict[str, int]]:
    techniques: Counter = Counter()
    tactics: Counter = Counter()
    kinds = {"technique": 0, "tactic_only": 0, "unreadable_only": 0}
    for labels in alerts.by_alert.values():
        ids = {label.upper() for label in labels if TECHNIQUE_ID.match(label.strip())}
        names = {normalize_label(label) for label in labels}
        named_tactics = names & TACTICS
        techniques.update(ids)
        tactics.update(named_tactics)
        if ids:
            kinds["technique"] += 1
        elif named_tactics:
            kinds["tactic_only"] += 1
        else:
            kinds["unreadable_only"] += 1
    return techniques, tactics, kinds


def _related_rules(
    technique: str, rules_by_technique: Mapping[str, Collection[int]]
) -> int:
    """Rules on its parent or subtechniques: they touch it without declaring it."""
    base = technique.split(".")[0]
    related = {
        rule_id
        for key, rule_ids in rules_by_technique.items()
        if key != technique and (key == base or key.startswith(f"{technique}."))
        for rule_id in rule_ids
    }
    return len(related)


def gap_report(
    context: str,
    alerts: AlertLabels,
    rules_by_technique: Mapping[str, Collection[int]],
    rules_by_tactic: Mapping[str, int],
    rule_counts: Mapping[str, int],
    siem_runs: Optional[Mapping[str, Mapping[str, int]]] = None,
) -> Dict[str, Any]:
    techniques, tactics, kinds = _classify(alerts)
    loaded = sum(rule_counts.values())
    unlabelled = alerts.total - len(alerts.by_alert)
    tactic_rules_known = any(rule_counts.get(s) for s in TACTIC_RULE_SOURCES)

    seen = [
        {
            "technique": technique,
            "alerts": count,
            "rules": len(set(rules_by_technique.get(technique, ()))),
            "related_rules": _related_rules(technique, rules_by_technique),
            # Runs results of the SIEM rules declaring it, counted.
            "siem_runs": dict((siem_runs or {}).get(technique, {})),
        }
        for technique, count in techniques.items()
    ]
    for row in seen:
        runs = row["siem_runs"]
        row["covered_on_paper_only"] = bool(runs) and set(runs) == {"fail"}
    seen.sort(key=lambda row: (row["rules"], row["related_rules"], -row["alerts"]))
    tactic_rows: List[Dict[str, Any]] = [
        {
            "tactic": tactic,
            "alerts": count,
            "rules_declaring": (
                rules_by_tactic.get(tactic, 0) if tactic_rules_known else None
            ),
        }
        for tactic, count in tactics.most_common()
    ]
    gaps = (
        [row for row in seen if row["rules"] == 0 and row["related_rules"] == 0]
        if loaded
        else []
    )
    paper_only = [row["technique"] for row in seen if row["covered_on_paper_only"]]

    unknown: List[str] = []
    missing = [source for source in RULE_SOURCES if not rule_counts.get(source)]
    if not loaded:
        unknown.append(
            "No detection rules are loaded, so no coverage can be stated. An operator "
            "adds rule sources in Settings, or runs scripts/setup_detection_repos.sh."
        )
    elif missing:
        unknown.append(
            f"No rules are loaded from {', '.join(missing)}. A gap here may be covered "
            "by rules in a source that is not installed."
        )
    if alerts.total == 0:
        unknown.append(
            f"No stored alert matches {context!r}. Vigil keeps no map from a threat "
            "name to techniques, so it cannot say what that threat would need."
            if context and not alerts.scope.startswith("case ")
            else "No stored alert is in scope."
        )
    if kinds["tactic_only"]:
        unknown.append(
            f"{kinds['tactic_only']} alert(s) name a tactic and no technique, so which "
            "technique ran is unknown and no technique gap can be stated for them. "
            "rules_declaring counts rules that declare the tactic, not rules that "
            "would have caught this activity."
        )
    if kinds["unreadable_only"]:
        unknown.append(
            f"{kinds['unreadable_only']} alert(s) carry only labels that are neither a "
            "technique nor a tactic, such as an unmapped class number."
        )
    if unlabelled:
        unknown.append(f"{unlabelled} alert(s) carry no ATT&CK label at all.")
    unknown.append(SIEMS_NOT_READ_NOTE)
    if tactics and not tactic_rules_known:
        unknown.append(
            "Tactic rule counts are unknown: only Sigma and Elastic rules declare a "
            "tactic, and neither is loaded."
        )

    if not loaded or not seen:
        answer = "unknown"
    elif gaps or paper_only:
        answer = "gaps_found"
    else:
        answer = "no_gaps_found"

    return {
        "context": context,
        "scope": alerts.scope,
        "answer": answer,
        "unknown": unknown,
        "rules_loaded": loaded,
        "rules_by_source": {s: rule_counts.get(s, 0) for s in RULE_SOURCES},
        "alerts_in_scope": alerts.total,
        "alerts_with_technique": kinds["technique"],
        "alerts_with_tactic_only": kinds["tactic_only"],
        "alerts_with_unreadable_label": kinds["unreadable_only"],
        "alerts_with_no_label": unlabelled,
        "techniques_analyzed": len(seen),
        "gaps_identified": len(gaps),
        "gaps": gaps,
        "covered_on_paper_only": paper_only,
        "techniques": seen,
        "tactics": tactic_rows,
    }
