"""ATT&CK coverage by technique and SIEM: the heat map (#4103).

One row per technique an active rule declares or a stored alert shows, in
ATT&CK tactic columns, with its state:

- ``proven``: an active SIEM copy of a rule declaring it passes both Runs and
  Sees, as ``catalog.siem_checks_by_technique`` counts ``proven`` (#3910);
- ``unproven``: an active rule declares it, but no SIEM copy of one is proven;
- ``no_rule``: no active rule that counts declares it: only a disabled, held
  or lint-failing rule does, or only alerts show it.

What counts is ``catalog.CREDITED``, as everywhere coverage is read: a rule
failing lint, or held by the import check (``import_check``: a new repo rule
or version not yet passed or approved), is no coverage. Each row says how many
held rules declare it, as it says for disabled ones.

Each row is split by SIEM (Elastic, Splunk, Wazuh): that SIEM's active copies
declaring it and how many are proven and caught. A rule counts once however many copies
it has (``catalog._rule_of_copy``, as the Rules page counts rules). A disabled
rule is no coverage, but each technique says how many disabled rules declare it,
so the map lists every technique the Rules page counts as declared (``mapped``).

Each row also says how many active rules were ``caught``: their copy's latest
version passed the caught check (``catalog.CAUGHT``: it alerted on a real
attack step, #3803). ``caught`` is a detail beside the state; it does
not change what proven means. ``emulate_next`` lists the techniques in the
order to emulate next: no rule, then unproven, then proven with nothing
caught, then the rest; most alerts seen first within each. It says nothing
about which tests an emulation runner holds (#5050).

Ported from Alex Margaris's heat map on the 1.x line (vigil-1.x d0a56ef6 and
7ee99b3f, ``core/detections/engineering/program.py::coverage``), without its
threat model and proposal replays, which main does not have: "proven" here is
main's Runs-and-Sees meaning, and a Sigma replay that passed on events Vigil
read from the SIEM (``checks.replay``, source ``siem``, #4212) is a detail on
the row; one on events the caller wrote is not counted. Read-only.

``read`` adds, only when asked, the map by detection layer (#5135): each
technique's state per layer (a SIEM whose rules Vigil reads, an alert source
that labels techniques, LogLM) and for the layers together, from the compose
emulation runs of the last ``caught.LOOKBACK_DAYS`` days, with the measures. It
replays those runs, so the map, the Day 1 checks and emulate-next never pay for
it: ``coverage()`` is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, select

from core.detections.inventory import catalog
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion
from core.storage.models.finding import Finding, FindingMitrePrediction
from core.threat_intel.mitre_lookup import attack_tactics, resolve_technique
from core.time import utcnow

logger = logging.getLogger(__name__)

SIEMS = ("elastic", "splunk", "wazuh")
UNMAPPED_TACTIC = "Unmapped"
# Defense Evasion, which ATT&CK 18 split into Stealth and Defense Impairment, is
# kept as a column for a rule that still declares it on an id ATT&CK lacks.
LEGACY_TACTICS = {"Stealth": "Defense Evasion"}
TECHNIQUE_ID = re.compile(r"^T\d{4}(\.\d{3})?$")
STATES = ("proven", "unproven", "no_rule")


def _copies(session: Any) -> List[Any]:
    """Each listed (active or disabled) copy's techniques, tactics, source and
    latest Runs, Sees, caught and replay, as the Rules page lists copies."""
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
            catalog.CAUGHT.label("caught"),
            catalog.CREDITED.label("credited"),
            catalog.HELD.label("held"),
            DetectionRuleVersion.checks["replay"]["result"].astext.label("replay"),
            DetectionRuleVersion.checks["replay"]["source"].astext.label(
                "replay_source"
            ),
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


def emulate_next(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows in the order to emulate next: no rule, unproven, proven with
    nothing caught, then the rest; within each, most alerts seen first, then
    the map's order."""

    def group(row: Dict[str, Any]) -> int:
        if row["state"] != "proven":
            return 0 if row["state"] == "no_rule" else 1
        return 3 if row["caught"] else 2

    return sorted(rows, key=lambda r: (group(r), -r["seen"], r["technique"]))


def coverage() -> Dict[str, Any]:
    """The heat map: each technique's state and SIEM split, the tactics in
    order, and the totals."""
    with get_db_manager().session_scope() as session:
        copies = _copies(session)
        seen = _seen(session)
        rule_of = catalog._rule_of_copy(session)
    rules: Dict[str, Set[Any]] = {}
    disabled: Dict[str, Set[Any]] = {}
    held: Dict[str, Set[Any]] = {}
    # Techniques an active rule declares that is no coverage (lint or held).
    uncounted: Set[str] = set()
    replayed: Dict[str, Set[Any]] = {}
    caught: Dict[str, Set[Any]] = {}
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
            if not copy.credited:
                uncounted.add(technique)
                if copy.held:
                    held.setdefault(technique, set()).add(rule)
                continue
            rules.setdefault(technique, set()).add(rule)
            if (
                copy.language == "sigma"
                and copy.replay == "pass"
                and copy.replay_source == "siem"
            ):
                replayed.setdefault(technique, set()).add(rule)
            if copy.caught == "pass":
                caught.setdefault(technique, set()).add(rule)
            if siem is None:
                continue
            tally = siems.setdefault(technique, {}).setdefault(
                siem, {"copies": 0, "proven": 0, "caught": 0, "failing": 0}
            )
            tally["copies"] += 1
            if copy.runs == copy.sees == "pass":
                tally["proven"] += 1
            if copy.caught == "pass":
                tally["caught"] += 1
            if "fail" in (copy.runs, copy.sees):
                tally["failing"] += 1
    order = tactic_order()
    columns = {_label(tactic): tactic for tactic in order}
    rows = []
    for technique in sorted({*rules, *disabled, *uncounted, *seen}):
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
                # Active rules declaring it held by the import check.
                "held": len(held.get(technique, set()) - rules.get(technique, set())),
                "seen": seen.get(technique, 0),
                "replayed": len(replayed.get(technique, ())),
                "caught": len(caught.get(technique, ())),
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
    listed_siems = {_siem_of(c.source) for c in copies}
    return {
        "techniques": rows,
        "tactics": tactics,
        # Every SIEM with a listed rule, so the lens hides none: one whose rules
        # declare no technique has no row to show it by.
        "siems": [s for s in SIEMS if s in listed_siems],
        "summary": {s: sum(1 for row in rows if row["state"] == s) for s in STATES},
        # Techniques with any caught rule, beside the states, not one of them.
        "caught_techniques": sum(1 for row in rows if row["caught"]),
        "emulate_next": [row["technique"] for row in emulate_next(rows)],
        "rule_total": len(
            {rule_of.get(c.id, (c.id,)) for c in copies if c.status == "active"}
        ),
        # Rules (active or disabled) declaring a technique: the Rules page's ``mapped``.
        "mapped": len({rule_of.get(c.id, (c.id,)) for c in copies if c.techniques}),
        "source": (
            "Active and disabled rules in the inventory (detection_rules, their "
            "latest version's checks) and alerts mapped to a technique "
            "(finding_mitre_predictions). Proven: an active SIEM copy passes Runs "
            "and Sees. Caught: an active rule's latest version passed the caught "
            "check (it alerted on a real attack step: a scored run, or a proven "
            "hunt or Case). The emulate-next order is no rule, then unproven, then "
            "proven with nothing caught, then the rest, most alerts seen first; it "
            "says nothing about which tests an emulation runner holds. A disabled "
            "rule, one failing lint, or one held by the import check (a new repo "
            "rule or version not yet passed or approved) is no coverage. A rule counts once however many copies it has."
        ),
    }


# --- By layer (#5135) ------------------------------------------------------

SCOPES = ("all", "emulated", "intel")
LOGLM = "loglm"
PROBE = "probe"
COMBINED = "combined"
NOT_CHECKED = "not_checked"
LAYER_SOURCE = (
    "Rows are detection layers keyed by a finding's data_source: one per SIEM "
    "with listed rules (elastic, splunk, wazuh; a Wazuh alert stored as elastic "
    "counts for wazuh by the rule it resolves to), one per other source whose "
    "findings in the window carry a technique label (finding_mitre_predictions), "
    "loglm when LogLM findings exist in the window, and the combined row last. "
    "Probe is never a layer; a source with findings and no labels is listed under "
    "unlabelled. Evidence is the compose emulation runs finished in the last "
    "window_days days, each step scored as the coverage report scores it (steps "
    "scored unknown count toward nothing). A step's citation credits a SIEM row "
    "only when it resolves to an active rule version whose rule declares the "
    "step's technique at parent level (Caught's rule); it credits an alert source "
    "or LogLM only when the cited finding's own predictions hold that technique at "
    "parent level. A step counts toward a technique when it is that technique or "
    "a sub-technique of it. The newest run in the window that scored the technique "
    "decides: every step credited is validated, some is partial, none is "
    "no_detection marked tested_missed; a SIEM row whose only catch is a shadow "
    "Caught pass in the window reads partial marked shadow. With no run: present "
    "(not validated) when an active rule in that SIEM declares it, the source "
    "labelled it on a finding in the window, or a LogLM finding predicted it in "
    "the window; else no_detection. A SIEM cell whose every declaring active copy "
    "fails Sees is marked not_observable (no other layer is ever marked). The "
    "combined row counts a step caught when any checked layer credited it, and is "
    "not_observable only when every shown layer is. A layer whose read failed "
    "reads not_checked with the reason and counts toward nothing. Measures are "
    "counts: validated per row, loglm_lift (only with a loglm row), validated by "
    "exactly one layer, present and never validated, and not observable by any "
    "layer. A run on a Case the caller can't read counts, its id withheld. Scope: "
    "all (the map's techniques), emulated (those emulated in the window), intel "
    "(those the connected threat intelligence's newest intrusion sets and reports "
    "name), or an explicit techniques list (any ids, including ones the map has "
    "no row for)."
)


@dataclass
class _Step:
    technique: str
    host: Optional[str]
    cites: List[Tuple[str, str]]  # (finding id, data_source), probe dropped


@dataclass
class _Run:
    run_id: str
    at: datetime
    steps: List[_Step] = field(default_factory=list)  # scored (not unknown)
    techniques: Set[str] = field(default_factory=set)  # every emulated one


def _parent(technique: str) -> str:
    return technique.strip().upper().split(".")[0]


def _toward(t: str, technique: str) -> bool:
    """``t`` counts toward ``technique``: the same id, or a sub-technique of it."""
    t = str(t).strip().upper()
    return t == technique or _parent(t) == technique


def _runs(episodes: List[Dict[str, Any]]) -> List[_Run]:
    """Each episode's scored steps, newest run first."""
    runs = []
    for episode in episodes:
        when = catalog._when(episode.get("concluded_at"))
        run = _Run(
            str(episode["investigation_id"]),
            when or datetime.min.replace(tzinfo=timezone.utc),
        )
        for row in episode["payload"].get("techniques") or []:
            technique = str(row["technique_id"]).strip().upper()
            run.techniques.add(technique)
            for step in row.get("steps") or []:
                if not isinstance(step, dict) or step.get("verdict") == "unknown":
                    continue
                cites = [
                    (str(c["finding_id"]), str(c.get("data_source") or ""))
                    for c in step.get("citations") or []
                    if isinstance(c, dict)
                    and c.get("finding_id")
                    and c.get("data_source") != PROBE
                ]
                run.steps.append(_Step(technique, step.get("hostname"), cites))
        runs.append(run)
    return sorted(runs, key=lambda r: r.at, reverse=True)


def _layer_siem(source: str, language: Optional[str]) -> Optional[str]:
    """The SIEM row a copy counts for: its own SIEM, or an overlay copy's language."""
    if source in SIEMS:
        return source
    if source == catalog.OVERLAY_SOURCE and language in SIEMS:
        return language
    return None


def _reads(runs: List[_Run], since: datetime) -> Dict[str, Any]:
    """Everything the layers are judged on, each read in its own session so one
    failing leaves the others standing; a failure is kept as its reason."""
    from core.detections.inventory import caught
    from core.workflows.workflow_run_service import run_hidden

    cited = {fid: ds for run in runs for s in run.steps for fid, ds in s.cites}
    out: Dict[str, Any] = {"errors": {}}
    with get_db_manager().session_scope() as session:
        out["copies"] = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.name,
                DetectionRule.source,
                DetectionRule.language,
                DetectionRule.techniques,
                catalog.RUNS.label("runs"),
                catalog.SEES.label("sees"),
                DetectionRuleVersion.checks["caught"].label("caught"),
                DetectionRuleVersion.checks["sees"]["data_source"].astext.label(
                    "blind"
                ),
            ).join(DetectionRuleVersion, catalog._latest_join())
            # A held or lint-failing rule is no coverage, here as everywhere.
            .where(catalog.ACTIVE, catalog.CREDITED)
        ).all()
        out["rule_of"] = catalog._rule_of_copy(session)
        # Counted, not named (#4359): run_unreadable's test, in this session.
        out["hidden"] = {r.run_id for r in runs if run_hidden(session, r.run_id)}
    try:
        with get_db_manager().session_scope() as session:
            # Per source, findings labelled with each id and with each parent
            # (a finding once however many of its labels match).
            labels: Dict[str, Dict[str, int]] = {}
            exact = func.upper(func.trim(FindingMitrePrediction.technique_id))
            for technique in (exact, func.split_part(exact, ".", 1)):
                for source, t, count in session.execute(
                    select(
                        Finding.data_source,
                        technique,
                        func.count(func.distinct(Finding.finding_id)),
                    )
                    .join(
                        FindingMitrePrediction,
                        FindingMitrePrediction.finding_id == Finding.finding_id,
                    )
                    .where(Finding.created_at >= since)
                    .group_by(Finding.data_source, technique)
                ):
                    if TECHNIQUE_ID.match(str(t)):
                        labels.setdefault(source, {})[str(t)] = count
            sources = set(
                session.scalars(
                    select(Finding.data_source)
                    .where(Finding.created_at >= since)
                    .distinct()
                )
            )
            predicted: Dict[str, Set[str]] = {}
            if cited:
                for fid, t in session.execute(
                    select(
                        FindingMitrePrediction.finding_id,
                        FindingMitrePrediction.technique_id,
                    ).where(FindingMitrePrediction.finding_id.in_(set(cited)))
                ):
                    predicted.setdefault(fid, set()).add(_parent(t))
        out.update(labels=labels, sources=sources, predicted=predicted)
    except Exception as exc:  # noqa: BLE001 -- those layers read not checked
        out["errors"]["labels"] = f"the findings' labels could not be read: {exc}"
        # The cited sources still get rows, read not checked; none is unlabelled.
        out.update(
            labels={ds: {} for ds in cited.values()},
            sources=set(cited.values()),
            predicted={},
        )
    try:
        with get_db_manager().session_scope() as session:
            resolved = {
                fid: r
                for fid, r in caught.resolve_cited(
                    session, {f for f, ds in cited.items() if ds != LOGLM}
                ).items()
                if r["resolved"] and r["status"] == "active"
            }
            rules = {
                rid: (source, language, {_parent(t) for t in techniques or []})
                for rid, source, language, techniques in session.execute(
                    select(
                        DetectionRule.id,
                        DetectionRule.source,
                        DetectionRule.language,
                        DetectionRule.techniques,
                    ).where(
                        DetectionRule.id.in_({r["rule_id"] for r in resolved.values()})
                    )
                )
            }
        out["fired"] = {
            fid: (_layer_siem(*rules[r["rule_id"]][:2]), rules[r["rule_id"]][2])
            for fid, r in resolved.items()
            if r["rule_id"] in rules
        }
    except Exception as exc:  # noqa: BLE001 -- the SIEM rows read not checked
        out["errors"]["siem"] = f"the rules that fired could not be resolved: {exc}"
        out["fired"] = {}
    return out


def _credited(step: _Step, reads: Dict[str, Any], siem_rows: Set[str]) -> Set[str]:
    """The layers a step's citations credit: a SIEM by Caught's rule, any other
    source (LogLM included) by the cited finding's own label."""
    parent = _parent(step.technique)
    layers = set()
    for fid, ds in step.cites:
        siem, declared = reads["fired"].get(fid, (None, set()))
        if siem in siem_rows and parent in declared:
            layers.add(siem)
        elif ds not in SIEMS and parent in reads["predicted"].get(fid, ()):
            layers.add(ds)
    return layers


def _state(caught: int, scored: int) -> Tuple[str, List[str]]:
    if caught == scored:
        return "validated", []
    return ("partial", []) if caught else ("no_detection", ["tested_missed"])


def _run_evidence(
    run: _Run, steps: List[_Step], caught: int, hidden: Set[str]
) -> Dict[str, Any]:
    restricted = run.run_id in hidden
    return {
        "run_id": None if restricted else run.run_id,
        **({"restricted": True} if restricted else {}),
        "finished_at": run.at.isoformat(),
        "hosts": sorted({s.host for s in steps if s.host}),
        "steps_caught": caught,
        "steps_run": len(steps),
    }


def _by_layer(
    report: Dict[str, Any],
    wanted: List[str],
    runs: List[_Run],
    emulation_error: Optional[str],
    since: datetime,
) -> Dict[str, Any]:
    reads = _reads(runs, since)
    errors = reads["errors"]
    by_id = {row["technique"]: row for row in report["techniques"]}
    siem_rows = [s for s in SIEMS if s in report["siems"]]
    siem_set = set(siem_rows)
    labels = reads["labels"]
    # A SIEM's source is never an alert row, listed rules or not: Wazuh's alerts
    # are stored as elastic and count for wazuh only through their rule.
    alert_rows = sorted(s for s in labels if s and s not in (*SIEMS, LOGLM, PROBE))
    loglm = LOGLM in reads["sources"]
    layers = [*siem_rows, *alert_rows, *([LOGLM] if loglm else [])]
    unlabelled = sorted(
        s for s in reads["sources"] - set(labels) - {*SIEMS, LOGLM, PROBE} if s
    )
    # Why a layer reads not checked: the emulation read is every layer's.
    failed: Dict[str, str] = {}
    for layer in layers:
        reason = emulation_error or errors.get(
            "siem" if layer in siem_set else "labels"
        )
        if reason:
            failed[layer] = reason

    copies: Dict[str, List[Any]] = {}
    for copy in reads["copies"]:
        siem = _layer_siem(copy.source, copy.language)
        if siem in siem_set:
            copies.setdefault(siem, []).append(copy)

    aware = since.replace(tzinfo=timezone.utc)

    def held(copy: Any) -> Dict[str, Any]:
        return copy.caught if isinstance(copy.caught, dict) else {}

    def shadow_at(copy: Any, technique: str) -> bool:
        """A shadow pass in the window, from a step of this technique (parent level)."""
        stored = held(copy)
        at = catalog._when(stored.get("at"))
        return bool(
            stored.get("result") == "pass"
            and stored.get("shadow")
            and _parent(str(stored.get("technique") or "")) == _parent(technique)
            and at
            and at >= aware
        )

    order = tactic_order()
    columns = {_label(t): t for t in order}
    rank = {t: i for i, t in enumerate(order)}
    rows = []
    without_loglm = 0  # combined validated with the loglm row left out
    for technique in wanted:
        known = by_id.get(technique)
        name, tactic = (
            (known["name"], known["tactic"])
            if known
            else _tactic_of(technique, {}, columns)
        )
        # The runs that scored it, newest first, each with its credited steps.
        scored = []
        for run in runs:
            steps = [s for s in run.steps if _toward(s.technique, technique)]
            if steps:
                scored.append(
                    (run, steps, [_credited(s, reads, siem_set) for s in steps])
                )
        cells: Dict[str, Dict[str, Any]] = {}
        for layer in layers:
            if layer in failed:
                cells[layer] = {"state": NOT_CHECKED, "reason": failed[layer]}
                continue
            evidence: Dict[str, Any] = {}
            marks: List[str] = []
            present = False
            if layer in siem_set:
                mine = [
                    c
                    for c in copies.get(layer, ())
                    if any(_toward(t, technique) for t in c.techniques or ())
                ]
                # Each declaring rule's checks; a Caught pass older than the
                # window, or one a newer run missed, stays here with its date.
                evidence["rules"] = [
                    {
                        "rule_id": c.id,
                        "name": c.name,
                        "runs": c.runs,
                        "sees": c.sees,
                        "caught": held(c).get("result"),
                        "caught_at": held(c).get("at"),
                        **({"shadow": True} if held(c).get("shadow") else {}),
                    }
                    for c in mine
                ]
                blind = sorted({c.blind or "unknown" for c in mine if c.sees == "fail"})
                if blind:
                    evidence["blind_spot"] = blind
                present = bool(mine)
                shadow = any(shadow_at(c, technique) for c in mine)
                observable = not mine or any(c.sees != "fail" for c in mine)
            else:
                flagged = labels.get(layer, {}).get(technique, 0)
                evidence["loglm_flags" if layer == LOGLM else "labelled"] = flagged
                present, shadow, observable = flagged > 0, False, True
            if scored:
                run, steps, credits = scored[0]
                caught = sum(layer in c for c in credits)
                state, marks = _state(caught, len(steps))
                evidence["newest_run"] = _run_evidence(
                    run, steps, caught, reads["hidden"]
                )
                evidence["older_runs"] = [
                    _run_evidence(r, s, sum(layer in c for c in cs), reads["hidden"])
                    for r, s, cs in scored[1:]
                ]
            else:
                state = "present" if present else "no_detection"
            if shadow and state in ("present", "no_detection"):
                state, marks = "partial", ["shadow"]
            if not observable and state in ("present", "no_detection"):
                marks.append("not_observable")
            cells[layer] = {"state": state, **({"marks": marks} if marks else {})}
            cells[layer]["evidence"] = evidence
        cells[COMBINED] = _combined(cells, scored, reads["hidden"])
        if LOGLM in cells:
            without = _combined(cells, scored, reads["hidden"], without=LOGLM)
            without_loglm += without["state"] == "validated"
        rows.append(
            {"technique": technique, "name": name, "tactic": tactic, "layers": cells}
        )
    rows.sort(key=lambda r: (rank.get(r["tactic"], len(order)), r["technique"]))

    def count(layer: str, state: str) -> int:
        return sum(r["layers"][layer]["state"] == state for r in rows)

    measures: Dict[str, Any] = {
        "techniques": len(rows),
        "validated": {
            layer: count(layer, "validated") for layer in [*layers, COMBINED]
        },
        "validated_by_one_layer": sum(
            sum(r["layers"][x]["state"] == "validated" for x in layers) == 1
            for r in rows
        ),
        "present_never_validated": sum(
            r["layers"][COMBINED]["state"] != "validated"
            and any(r["layers"][x]["state"] == "present" for x in layers)
            for r in rows
        ),
        "not_observable": sum(
            "not_observable" in r["layers"][COMBINED].get("marks", ()) for r in rows
        ),
    }
    if LOGLM in layers:
        measures["loglm_lift"] = count(COMBINED, "validated") - without_loglm
    rule_of = reads["rule_of"]
    return {
        "window_days": (utcnow() - since).days,
        "layers": [
            _layer_row(
                layer,
                siem_set,
                {rule_of.get(c.id, (c.id,)) for c in copies.get(layer, ())},
                failed.get(layer),
            )
            for layer in layers
        ]
        + [
            {
                "layer": COMBINED,
                "kind": COMBINED,
                "reads": "Every checked layer together.",
            }
        ],
        "unlabelled": unlabelled,
        "emulation": {
            "runs": len(runs),
            **({"error": emulation_error} if emulation_error else {}),
            **(
                {
                    "note": (
                        f"No compose emulation run finished in the last "
                        f"{(utcnow() - since).days} days with a scored step, so "
                        "nothing reads validated."
                    )
                }
                if not any(r.steps for r in runs)
                else {}
            ),
        },
        "techniques": rows,
        "tactics": [t for t in order if any(r["tactic"] == t for r in rows)],
        "measures": measures,
    }


def _combined(
    cells: Dict[str, Dict[str, Any]],
    scored: List[Tuple[_Run, List[_Step], List[Set[str]]]],
    hidden: Set[str],
    without: Optional[str] = None,
) -> Dict[str, Any]:
    """The layers together: a step caught when any checked layer credited it."""
    shown = {
        layer: cell
        for layer, cell in cells.items()
        if layer not in (COMBINED, without) and cell["state"] != NOT_CHECKED
    }
    if not shown:
        return {"state": NOT_CHECKED, "reason": "no layer could be read"}
    states = {cell["state"] for cell in shown.values()}
    evidence: Dict[str, Any] = {}
    if scored:
        run, steps, credits = scored[0]
        caught = sum(bool(c & set(shown)) for c in credits)
        state, marks = _state(caught, len(steps))
        evidence["newest_run"] = _run_evidence(run, steps, caught, hidden)
        if state == "no_detection" and "partial" in states:
            state, marks = "partial", ["shadow"]  # a SIEM's shadow catch
    elif "partial" in states:
        state, marks = "partial", ["shadow"]
    else:
        state, marks = ("present" if "present" in states else "no_detection"), []
    if all("not_observable" in c.get("marks", ()) for c in shown.values()):
        marks.append("not_observable")
    return {"state": state, **({"marks": marks} if marks else {}), "evidence": evidence}


def _layer_row(
    layer: str, siems: Set[str], rules: Set[Any], reason: Optional[str]
) -> Dict[str, Any]:
    if layer in siems:
        row = {
            "layer": layer,
            "kind": "siem",
            "reads": f"Alerts its active rules raise in {layer}, credited only "
            "when the rule declares the technique.",
            "rules": len(rules),
        }
    elif layer == LOGLM:
        row = {
            "layer": layer,
            "kind": LOGLM,
            "reads": "LogLM findings and the techniques they predict.",
            "observable": "LogLM declares no list of the techniques it can "
            "observe, so no LogLM cell reads not observable.",
        }
    else:
        row = {
            "layer": layer,
            "kind": "alerts",
            "reads": f"Findings from {layer} and the techniques they are labelled with.",
        }
    if reason:
        row.update(state=NOT_CHECKED, reason=reason)
    return row


def technique_ids(techniques: Iterable[str]) -> List[str]:
    """The ids given, upper-cased, in order, once each; a bad id is refused."""
    out = []
    for raw in techniques:
        for t in str(raw).split(","):
            t = t.strip().upper()
            if not t:
                continue
            if not TECHNIQUE_ID.match(t):
                raise ValueError(
                    f"{t!r} is not an ATT&CK technique id (T1234 or T1234.001)"
                )
            if t not in out:
                out.append(t)
    return out


async def by_layer(
    report: Dict[str, Any],
    scope: str = "all",
    techniques: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """The map by layer for ``scope`` or the ``techniques`` given, from
    ``report`` (``coverage()``) and the window's compose emulation runs."""
    from core.detections.inventory import caught
    from core.memory.learning_episodes import emulation_episodes

    wanted = technique_ids(techniques or ())
    if wanted:
        scope = "techniques"
    elif scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    head: Dict[str, Any] = {"scope": scope}
    if scope == "intel":
        from core.detections.gap_run import recent_intel

        try:
            intel = await recent_intel()
        except Exception as exc:  # noqa: BLE001 -- read as not connected
            logger.warning("Coverage by layer: threat intel not read: %s", exc)
            intel = {}
        if not intel:
            return {
                **head,
                "intel": {"connected": False},
                "note": "No threat intelligence is connected (OpenCTI is off, not "
                "set up or not answering), so there are no techniques to answer for.",
                "source": LAYER_SOURCE,
            }
        head["intel"] = {"connected": True, "named_by": intel}
        wanted = sorted(intel)
    end = datetime.now(timezone.utc)
    error = None
    try:
        episodes = await emulation_episodes(
            end - timedelta(days=caught.LOOKBACK_DAYS), end
        )
    except Exception as exc:  # noqa: BLE001 -- every layer reads not checked
        episodes, error = [], f"the emulation runs could not be read: {exc}"
    runs = _runs(episodes)
    if scope == "emulated":
        wanted = sorted({t for r in runs for t in r.techniques})
    elif scope == "all":
        wanted = [row["technique"] for row in report["techniques"]]
    since = utcnow() - timedelta(days=caught.LOOKBACK_DAYS)
    body = await asyncio.to_thread(_by_layer, report, wanted, runs, error, since)
    return {**head, **body, "source": LAYER_SOURCE}


async def read(
    layers: bool = False,
    scope: str = "all",
    techniques: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """``coverage()``, with ``by_layer`` added only when asked for (``layers``,
    a scope other than all, or a techniques list). A bad id raises ValueError
    before anything is read."""
    techniques = technique_ids(techniques or ())
    report = await asyncio.to_thread(coverage)
    if layers or scope != "all" or techniques:
        report["by_layer"] = await by_layer(report, scope, techniques)
    return report
