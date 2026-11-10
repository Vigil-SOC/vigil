"""Which ATT&CK labels stored alerts carry, set against the active detection rules.

A gap is a technique seen on a stored alert that no active rule declares; rules are
counted by detection_rules id. Stored labels are not all techniques: ingest keeps
whatever key a source sent, so a row may name a tactic or an unmapped class. Those
cannot prove a gap, and the report says so rather than guessing which technique ran.

A technique whose SIEM rules (read from Kibana, Splunk and Wazuh) each fail Runs or
Sees is covered on paper only: a rule declares it, and nothing that runs on arriving
data does. A Blind spot is a data source SIEM rules read that has no events (or lacks
their fields), with the rules it starves.

A proven hunt or closed Case that no rule alerted on is a run rules missed. It is
read from the Verdicts episodic memory already holds and correlated with
``reconstruct`` against the Findings in its window; nothing is written.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import String, cast

from core.detections.reconstruction import _entities_from_context, reconstruct
from core.memory.entity_keys import normalise_keys
from core.storage.connection import get_db_manager
from core.storage.models import (
    Case,
    EpisodicDistilMarker,
    EpisodicVerdict,
    Finding,
    FindingMitrePrediction,
    WorkflowRun,
    case_findings,
)
from core.threat_intel.mitre_lookup import attack_techniques, attack_techniques_version

RULE_SOURCES = ("sigma", "splunk", "elastic", "kql", "wazuh")
# The rule formats that declare a tactic; Splunk, KQL and Wazuh rules name techniques only.
TACTIC_RULE_SOURCES = ("sigma", "elastic")

SIEMS_NOT_READ_NOTE = (
    "Elastic (Kibana), Splunk and Wazuh rules are read from a SIEM. Kibana, "
    "Wazuh and Splunk rules get Sees (a Wazuh rule's data source is its decoder, "
    "a Splunk search's its index/sourcetype or data model) and Quiet checks; "
    "Kibana, Wazuh and Splunk rules also get Noisy and Silent."
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


def read_alert_labels(
    context: str = "", since: Optional[datetime] = None
) -> AlertLabels:
    """Scope by ``context``: empty is every alert, a case id is that case, else a label
    match; with ``since``, only alerts stored at or after it."""
    with get_db_manager().session_scope() as session:
        is_case = bool(context) and session.get(Case, context) is not None
        rows = session.query(
            FindingMitrePrediction.finding_id, FindingMitrePrediction.technique_id
        )
        total = session.query(Finding.finding_id)
        if since is not None:
            stored_since = session.query(Finding.finding_id).filter(
                Finding.created_at >= _naive_utc(since)
            )
            rows = rows.filter(
                FindingMitrePrediction.finding_id.in_(stored_since.subquery().select())
            )
            total = total.filter(Finding.created_at >= _naive_utc(since))
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
    siem_checks: Optional[Mapping[str, Mapping[str, Any]]] = None,
    blind_spots: Optional[List[Dict[str, Any]]] = None,
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
            # Runs and Sees results of the SIEM rules declaring it, counted.
            "siem_runs": dict((siem_checks or {}).get(technique, {}).get("runs", {})),
            "siem_sees": dict((siem_checks or {}).get(technique, {}).get("sees", {})),
            # Every deployed SIEM rule on it fails Runs or Sees.
            "covered_on_paper_only": bool(
                (checks := (siem_checks or {}).get(technique))
                and checks["rules"]
                and checks["failing"] == checks["rules"]
            ),
        }
        for technique, count in techniques.items()
    ]
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
            "name to techniques: pass the techniques a threat uses (e.g. from threat "
            "intel) as techniques to check each against the rules."
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
        "blind_spots": blind_spots or [],
        "techniques": seen,
        "tactics": tactic_rows,
    }


# --- Techniques asked about --------------------------------------------------

TECHNIQUE_BUCKETS = ("proven", "siem_unproven", "repo_only", "none")


def technique_check(
    techniques: Collection[Any],
    rules_by_technique: Mapping[str, Collection[int]],
    siem_checks: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Each named technique in one bucket: proven (a SIEM rule passes Runs and Sees),
    siem_unproven (SIEM rules, none passing both), repo_only (only repo or overlay
    rules) or none. Every row that is not proven is a gap. A well-formed id that is not
    an active technique in the pinned ATT&CK release is listed in ``unknown`` with its
    reason (revoked ids name their replacement; deprecated ids say so): it is never a
    row, a gap or a count."""
    rows: List[Dict[str, Any]] = []
    rejected: List[str] = []
    unknown: List[Dict[str, str]] = []
    catalog_ids = attack_techniques()
    for raw in dict.fromkeys(str(t).strip() for t in techniques if str(t).strip()):
        if not TECHNIQUE_ID.match(raw):
            rejected.append(raw)
            continue
        technique = raw.upper()
        if any(row["technique"] == technique for row in rows) or any(
            u["technique"] == technique for u in unknown
        ):
            continue
        reason = _not_active_reason(catalog_ids.get(technique))
        if reason:
            unknown.append({"technique": technique, "reason": reason})
            continue
        checks = siem_checks.get(technique, {})
        rules = len(set(rules_by_technique.get(technique, ())))
        if checks.get("proven"):
            bucket = "proven"
        elif checks.get("rules"):
            bucket = "siem_unproven"
        elif rules:
            bucket = "repo_only"
        else:
            bucket = "none"
        rows.append(
            {
                "technique": technique,
                "bucket": bucket,
                "rules": rules,
                "siem_rules": checks.get("rules", 0),
                "siem_proven_rules": checks.get("proven", 0),
                # Rules on its parent or subtechniques: reported, never proof.
                "related_rules": _related_rules(technique, rules_by_technique),
            }
        )
    return {
        "checked": len(rows),
        "by_bucket": {
            b: sum(r["bucket"] == b for r in rows) for b in TECHNIQUE_BUCKETS
        },
        "gaps": [r["technique"] for r in rows if r["bucket"] != "proven"],
        "rows": rows,
        "rejected": rejected,
        "unknown": unknown,
    }


def _is_listable(technique: str) -> bool:
    """False for a well-formed id that is not an active ATT&CK technique."""
    if not TECHNIQUE_ID.match(technique):
        return True
    return not _not_active_reason(attack_techniques().get(technique.upper()))


def _not_active_reason(entry: Optional[Mapping[str, Any]]) -> str:
    """Why an id is not an active ATT&CK technique; empty when it is."""
    if entry is None:
        return f"not an ATT&CK technique (release {attack_techniques_version()})"
    if entry["status"] == "revoked":
        replacement = entry.get("replaced_by")
        return f"revoked; replaced by {replacement}" if replacement else "revoked"
    if entry["status"] == "deprecated":
        return "deprecated"
    return ""


# --- Runs rules missed -------------------------------------------------------

LEDGER_KINDS = ("hunt", "case")
RULED_OUT = ("disproven", "false_positive")
# A Verdict's entity keys, as the reconstruction step spells them. Other key types
# (hash, domain, url...) are not entities a Finding is joined on.
_STEP_KEYS = {"host": "hostnames", "ip": "src_ips", "user": "users"}
# One Findings read per Verdict window; a full read may have been cut off.
FINDINGS_LIMIT = 10000

FindingsReader = Callable[[datetime, datetime], List[Dict[str, Any]]]


def _naive_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    )


def read_ledger(since_days: int = 30) -> List[Dict[str, Any]]:
    """Hunt and Case Verdicts concluded in the window, Case gaps filled from its Findings.

    A Case Verdict carries no techniques and usually no subjects, so both are read
    from the Case's own Findings here, and techniques also from the hunt Verdicts
    that ran on the Case; nothing is written back to the Verdict. A ``handed_off``
    outcome reads as ``proven``: a hunt can only hand off a proven hypothesis.
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    with get_db_manager().session_scope() as session:
        query = session.query(EpisodicVerdict).filter(
            EpisodicVerdict.investigation_kind.in_(LEDGER_KINDS),
            EpisodicVerdict.concluded_at >= since,
        )
        verdicts = [
            {
                "investigation_kind": v.investigation_kind,
                "investigation_id": v.investigation_id,
                "hypothesis_id": v.hypothesis_id,
                "outcome": "proven" if v.outcome == "handed_off" else v.outcome,
                "trust": v.trust,
                "techniques": {t.strip().upper() for t in v.techniques or []},
                "subjects": list(v.subject_entities or []),
                "first_seen": _naive_utc(v.first_seen),
                "last_seen": _naive_utc(v.last_seen),
                "concluded_at": v.concluded_at,
            }
            for v in query.all()
        ]
        case_ids = {
            v["investigation_id"] for v in verdicts if v["investigation_kind"] == "case"
        }
        labels: Dict[str, set] = {}
        contexts: Dict[str, List[Dict[str, Any]]] = {}
        if case_ids:
            for case_id, label in (
                session.query(
                    case_findings.c.case_id, FindingMitrePrediction.technique_id
                )
                .join(
                    FindingMitrePrediction,
                    FindingMitrePrediction.finding_id == case_findings.c.finding_id,
                )
                .filter(case_findings.c.case_id.in_(case_ids))
            ):
                if TECHNIQUE_ID.match(label.strip()):
                    labels.setdefault(case_id, set()).add(label.strip().upper())
            for case_id, entity_context in (
                session.query(case_findings.c.case_id, Finding.entity_context)
                .join(Finding, Finding.finding_id == case_findings.c.finding_id)
                .filter(case_findings.c.case_id.in_(case_ids))
            ):
                if isinstance(entity_context, dict):
                    contexts.setdefault(case_id, []).append(entity_context)
            # A hunt run on the Case (trigger_context.case_id) lends its techniques,
            # whenever it concluded; a ruled-out one lends none.
            run_case_id = WorkflowRun.trigger_context["case_id"].astext
            for case_id, techniques in (
                session.query(run_case_id, EpisodicVerdict.techniques)
                .join(
                    EpisodicDistilMarker,
                    (
                        EpisodicDistilMarker.investigation_kind
                        == EpisodicVerdict.investigation_kind
                    )
                    & (
                        EpisodicDistilMarker.investigation_id
                        == EpisodicVerdict.investigation_id
                    ),
                )
                .join(
                    WorkflowRun,
                    WorkflowRun.run_id
                    == cast(EpisodicDistilMarker.origin_run_id, String),
                )
                .filter(
                    EpisodicVerdict.investigation_kind == "hunt",
                    EpisodicVerdict.outcome.notin_(RULED_OUT),
                    run_case_id.in_(case_ids),
                )
            ):
                for t in techniques or []:
                    labels.setdefault(case_id, set()).add(t.strip().upper())

    for v in verdicts:
        if v["investigation_kind"] != "case":
            v["entities"] = _subject_entities(v["subjects"])
            continue
        case_id = v["investigation_id"]
        v["techniques"] |= labels.get(case_id, set())
        v["entities"] = _subject_entities(v["subjects"])
        if not any(v["entities"].values()):
            # Only when the Verdict names no subject: its own keys win.
            for entity_context in contexts.get(case_id, []):
                for key, values in _entities_from_context(entity_context).items():
                    v["entities"][key] += values
    return verdicts


def _subject_entities(keys: List[str]) -> Dict[str, List[str]]:
    entities: Dict[str, List[str]] = {key: [] for key in _STEP_KEYS.values()}
    for key in normalise_keys(keys):
        kind, _, value = key.partition(":")
        if kind in _STEP_KEYS:
            entities[_STEP_KEYS[kind]].append(value)
    return entities


def _run(v: Mapping[str, Any]) -> Tuple[str, str]:
    return v["investigation_kind"], v["investigation_id"]


def _run_row(run: Tuple[str, str]) -> Dict[str, str]:
    return {"investigation_kind": run[0], "investigation_id": run[1]}


def reconstruct_verdict(
    v: Mapping[str, Any], read_findings: FindingsReader
) -> Tuple[Optional[str], Dict[str, Any]]:
    """A Verdict as one step reconstructed against its window's Findings: (why
    it cannot be correlated, {}) or (None, the step's record with its citations)."""
    if not any(v["entities"].values()):
        return "names no host, IP or user, so no Finding can be joined to it", {}
    findings = read_findings(v["first_seen"], v["last_seen"])
    if len(findings) >= FINDINGS_LIMIT:
        return (
            f"its window holds {FINDINGS_LIMIT}+ Findings and the read was "
            "cut off, so whether a rule alerted is unknown"
        ), {}
    step = {**v["entities"], "started_at": v["first_seen"], "ended_at": v["last_seen"]}
    # A probe is Vigil checking itself, not a rule alerting.
    alerts = [f for f in findings if f.get("data_source") != "probe"]
    return None, reconstruct([step], alerts)["steps"][0]


def missed_runs_report(
    context: str,
    is_case: bool,
    verdicts: List[Dict[str, Any]],
    read_findings: FindingsReader,
    rules_by_technique: Mapping[str, Collection[int]],
    since_days: int,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Proven runs no rule alerted on, grouped by technique.

    A run is listed in a group when one of its proven Verdicts on that technique
    reconstructs as ``missed`` or ``loglm``. A Verdict that cannot be correlated
    (no host/IP/user, or a Findings read that was cut off) is unknown, never missed.
    With ``since``, only proven Verdicts concluded at or after it are reconstructed;
    ruled-out runs are still read across the whole window.
    """
    # Hunt Verdicts store techniques unchecked: an id that is not an active ATT&CK
    # technique never reaches a group, so a made-up one cannot become a Case.
    verdicts = [
        {**v, "techniques": {t for t in v["techniques"] if _is_listable(t)}}
        for v in verdicts
    ]
    # A case id scopes to that Case's Verdict, any other context to matching
    # techniques. Ruled-out runs are read across the whole window either way.
    wanted = normalize_label(context) if context and not is_case else ""

    def in_scope(technique: str) -> bool:
        return not wanted or wanted in normalize_label(technique)

    proven = [
        v
        for v in verdicts
        if v["outcome"] == "proven"
        and (since is None or _naive_utc(v["concluded_at"]) >= _naive_utc(since))
        and (not is_case or _run(v) == ("case", context))
        and (not wanted or any(in_scope(t) for t in v["techniques"]))
    ]
    unknown: List[Dict[str, Any]] = []
    missed: List[Dict[str, Any]] = []
    caught = 0
    for v in proven:
        reason, record = reconstruct_verdict(v, read_findings)
        if reason:
            unknown.append(
                {
                    **_run_row(_run(v)),
                    "hypothesis_id": v["hypothesis_id"],
                    "reason": reason,
                }
            )
            continue
        verdict = record["verdict"]
        if verdict in ("missed", "loglm"):
            missed.append(v)
        else:
            caught += 1

    groups: Dict[str, Dict[str, Any]] = {}
    without_technique = set()
    for v in missed:
        if not v["techniques"]:
            without_technique.add(_run(v))
        for technique in v["techniques"]:
            if not in_scope(technique):
                continue
            group = groups.setdefault(
                technique, {"runs": {}, "confirmed": set(), "last": None}
            )
            group["runs"].setdefault(_run(v), v["concluded_at"])
            group["runs"][_run(v)] = max(group["runs"][_run(v)], v["concluded_at"])
            if v["trust"] == "analyst":
                group["confirmed"].add(_run(v))
    # Proven runs that also count a technique-less Verdict are not technique-less.
    without_technique -= {_run(v) for v in missed if v["techniques"]}

    ruled_out: Dict[str, set] = {}
    for v in verdicts:
        if v["outcome"] in RULED_OUT:
            for technique in v["techniques"]:
                ruled_out.setdefault(technique, set()).add((*_run(v), v["outcome"]))

    rows = [
        {
            "technique": technique,
            "runs": [_run_row(run) for run in sorted(group["runs"])],
            "confirmed": len(group["confirmed"]),
            # Which of ``runs`` a person confirmed (a Verdict with analyst trust).
            "confirmed_runs": [_run_row(run) for run in sorted(group["confirmed"])],
            "rules": len(set(rules_by_technique.get(technique, ()))),
            "ruled_out": [
                {**_run_row((kind, run_id)), "outcome": outcome}
                for kind, run_id, outcome in sorted(ruled_out.get(technique, ()))
            ],
            "last_concluded_at": max(group["runs"].values()).isoformat(),
        }
        for technique, group in groups.items()
    ]
    rows.sort(key=lambda r: r["last_concluded_at"], reverse=True)
    rows.sort(key=lambda r: (r["confirmed"], len(r["runs"])), reverse=True)
    listed = [r for r in rows if len(r["runs"]) >= 2 or r["confirmed"] >= 1]

    return {
        "since_days": since_days,
        "proven_verdicts": len(proven),
        "verdicts_caught_by_rule": caught,
        "missed_runs": len({_run(v) for v in missed}),
        "missed_runs_without_technique": len(without_technique),
        "groups": listed,
        "groups_not_listed": len(rows) - len(listed),
        "unknown": unknown,
    }
