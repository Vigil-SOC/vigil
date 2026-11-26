"""Step confidence (#5265): one score for a step an investigation takes on its own.

A step is a dismissal (the run concluded benign or false_positive and the case is
closed, or held for a person) or a containment the run proposed. The score is
worked out from stored records only: no model call and nothing per vendor.

The rule, version 1. Three factors, each a number from 0 to 1 or unknown:

- **Precedent.** The step's kind is that of the case's earliest finding (by
  timestamp, then stored time): the detection rule that fired it when the
  catalog resolves one, else its (data source, title). This is the grouping of
  ``_groups`` in ``core/reporting/production_tuning.py``. For a rule, the counts
  are those the Accurate sweep stored on the rule's latest version
  (``checks['accurate']``); for a title they are counted now, over every stored
  finding of that source and title that resolves to no rule, leaving out sample
  and probe data. Only people's determinations count: ``analyst`` is how many
  findings a person judged, ``false_person`` how many of those the person called
  not real. A dismissal agrees with "not real" (``false_person``), a containment
  with "real" (``analyst - false_person``). The value is the 95% Wilson lower
  bound of agreeing out of ``analyst``; nobody judged gives 0.
- **Blind spots.** The agent counts the calls that the estate failed (timeout,
  unavailable, backend error) to a tool answering a required need, where no
  later call to the same tool was served. Any such call gives 0, none gives 1;
  a run that did not say is unknown.
- **Run quality.** From the terminal's ``capped``, ``citation_count`` and
  ``grounded``: 0 when the run was capped, cited nothing it holds, or its review
  was not grounded; 1 when all three were said and none flags; else unknown.

Which factors are in force is read from the autonomy ceiling (#5277): the
``confidence.*`` switches in INTENT.md or on Settings › Limits & autonomy, each
on unless switched off. An off factor's inputs are still worked out and stored
for display; it never enters the score.

The score is the lowest factor in force. It is unknown when a factor in force is
unknown, unless a known one is already 0. With no factor in force nothing is left
to lower it: the value is 1, the band is ``none`` ("no factors in force"), and
there is no lowest factor and no raise line. That is a person's choice to count
nothing, not an unknown, so switching factors off never lowers a score or makes
a known one unknown. The band is display only. The line on what
would raise the score is built from the lowest factor's inputs, never from text
the model wrote. Each score records the rule version, the factors in force and
the applied INTENT.md revision, so it can be worked again from the same records.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select

RULE_VERSION = 1
DISMISSAL = "dismissal"
CONTAINMENT = "containment"
FACTORS = ("precedent", "blind_spots", "run_quality")
# Display only: nothing reads the band to decide.
BANDS = ((0.8, "high"), (0.5, "medium"), (0.0, "low"))
# The band when every factor is switched off: "no factors in force", not unknown.
NONE_IN_FORCE = "none"

logger = logging.getLogger(__name__)


def score(step: str, case_id: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    """The step's score. ``facts`` is ``TerminalUpdate.standing()``: the run's
    three facts, its ``blind_spots`` count and ``unserved_tools``, None where unsaid."""
    from core.response.ceiling import factors_in_force, read_ceiling
    from core.response.config import applied_intent

    blind_spots = facts.get("blind_spots")
    factors = {
        "precedent": precedent(step, case_id),
        "blind_spots": {
            "value": None if blind_spots is None else (0.0 if blind_spots else 1.0),
            "count": blind_spots,
            "tools": facts.get("unserved_tools") or [],
        },
        "run_quality": run_quality(facts),
    }
    in_force = factors_in_force(read_ceiling())
    if in_force:
        value, lowest = combine({name: factors[name]["value"] for name in in_force})
    else:
        value, lowest = 1.0, None  # nothing in force: see the module docstring
    return {
        "step": step,
        "value": value,
        "band": band(value) if in_force else NONE_IN_FORCE,
        "lowest": lowest,
        "raise": raise_line(lowest, factors[lowest]) if lowest else None,
        "factors": factors,
        "factors_in_force": in_force,
        "rule_version": RULE_VERSION,
        "intent_revision": applied_intent().get("revision"),
    }


def combine(values: Dict[str, Optional[float]]) -> tuple:
    """(score, lowest factor): the minimum, unknown when a factor is and none is 0."""
    known = {name: v for name, v in values.items() if v is not None}
    unknown = [name for name, v in values.items() if v is None]
    lowest = min(known, key=known.get) if known else None
    if lowest is not None and known[lowest] == 0:
        return 0.0, lowest
    if unknown:
        return None, unknown[0]
    return (known[lowest], lowest) if lowest else (None, None)


def band(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    return next(name for floor, name in BANDS if value >= floor)


def quote(step_score: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What an activity or the trail quotes: value, band, lowest factor and the
    factors in force, so a score with none in force reads as that."""
    if not step_score:
        return None
    return {
        k: step_score.get(k)
        for k in ("value", "band", "lowest", "factors_in_force", "rule_version")
    }


def run_quality(facts: Dict[str, Any]) -> Dict[str, Any]:
    capped, count, grounded = (
        facts.get("capped"),
        facts.get("citation_count"),
        facts.get("grounded"),
    )
    flagged = [
        name
        for name, hit in (
            ("capped", capped is True),
            ("cited_nothing", count == 0),
            ("not_grounded", grounded is False),
        )
        if hit
    ]
    said = None not in (capped, count, grounded)
    value = 0.0 if flagged else (1.0 if said else None)
    return {
        "value": value,
        "capped": capped,
        "citation_count": count,
        "grounded": grounded,
        "flagged": flagged,
    }


def precedent(step: str, case_id: str) -> Dict[str, Any]:
    from core.reporting.analytics_service import _wilson
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        kind = _kind(session, case_id)
        counts = _counts(session, kind) if kind else None
    analyst = int((counts or {}).get("analyst") or 0)
    false_person = int((counts or {}).get("false_person") or 0)
    agreeing = false_person if step == DISMISSAL else analyst - false_person
    bounds = _wilson(agreeing, analyst)
    return {
        "value": bounds[0] if bounds else 0.0,
        "kind": kind,
        "analyst": analyst,
        "false_person": false_person,
        "agreeing": agreeing,
        # A rule the Accurate sweep has not checked yet has no stored counts.
        **(
            {"checked_at": counts.get("checked_at")}
            if kind and kind["kind"] == "rule" and counts
            else {}
        ),
    }


def _kind(session: Any, case_id: str) -> Optional[Dict[str, Any]]:
    """The kind of the case's earliest finding, as ``_groups`` keys it."""
    from core.detections.inventory import catalog
    from core.detections.inventory.accurate import _findings
    from core.reporting.production_tuning import _groups
    from core.storage.models import Finding, case_findings

    first = session.execute(
        select(Finding.finding_id, Finding.title)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .where(case_findings.c.case_id == case_id)
        .order_by(
            Finding.timestamp.asc().nulls_last(),
            Finding.created_at.asc(),
            Finding.finding_id,
        )
        .limit(1)
    ).first()
    if first is None:
        return None
    fid, title = first
    found = _findings(session, Finding.finding_id == fid, sources=None)
    resolved = catalog.resolve_findings(session, found, full=False)
    (group,) = _groups(found, {fid: title}, resolved, {}).values()
    if group["kind"] == "rule":
        return {"kind": "rule", "rule_id": group["rule_id"], "name": group["name"]}
    return {"kind": "title", "source": group["source"], "title": group["title"]}


def _counts(session: Any, kind: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """People's judgements of the kind: ``analyst``, ``false_person``."""
    from core.storage.models import DetectionRule, DetectionRuleVersion

    if kind["kind"] == "rule":
        checks = session.execute(
            select(DetectionRuleVersion.checks)
            .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
            .where(
                DetectionRule.id == kind["rule_id"],
                DetectionRuleVersion.version == DetectionRule.latest_version,
            )
        ).scalar()
        accurate = (checks or {}).get("accurate")
        return accurate if isinstance(accurate, dict) else None
    return _title_counts(session, kind["source"], kind["title"])


def _title_counts(session: Any, source: str, title: Optional[str]) -> Dict[str, Any]:
    from core.detections.inventory import catalog
    from core.detections.inventory.accurate import _closes, _findings
    from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
    from core.reporting.production_tuning import PROBE, _groups
    from core.storage.models import Finding

    if source in (SAMPLE_DATA_SOURCE, PROBE):
        return {"analyst": 0, "false_person": 0}
    where = [
        Finding.data_source == source,
        Finding.title.is_(None) if title is None else Finding.title == title,
    ]
    found = _findings(session, *where, sources=None)
    resolved = catalog.resolve_findings(session, found, full=False) if found else {}
    closes = _closes(session, *where, sources=None)
    groups = _groups(found, dict.fromkeys(found, title), resolved, closes)
    group = groups.get(("title", source, title)) or {}
    # Vigil's own closes are "agent" in _groups and never reach these two.
    return {
        "analyst": group.get("analyst", 0),
        "false_person": group.get("false_person", 0),
    }


def raise_line(lowest: str, factor: Dict[str, Any]) -> str:
    """What would raise the score, from the lowest factor's own inputs."""
    if lowest == "precedent":
        kind = factor.get("kind")
        if kind is None:
            return "The case has no finding, so no kind people have judged."
        if kind["kind"] == "rule" and "checked_at" not in factor:
            return (
                "No Accurate counts are stored for this rule yet; "
                "the Accurate sweep writes them."
            )
        return (
            f"People judged {factor['analyst']} alerts of this kind and "
            f"{factor['agreeing']} agree with this step; more people's "
            "determinations that agree would raise it."
        )
    if lowest == "blind_spots":
        if factor.get("count") is None:
            return "The run did not say whether any tool failed; a run that says would."
        tools = ", ".join(factor.get("tools") or []) or "a tool"
        return (
            f"{factor['count']} call(s) to {tools} failed and were never served; "
            "a run whose calls are served would raise it."
        )
    flagged = factor.get("flagged") or []
    if flagged:
        return f"The run flagged: {', '.join(flagged)}; a run without these would raise it."
    unsaid = [
        k for k in ("capped", "citation_count", "grounded") if factor.get(k) is None
    ]
    return f"The run did not say {', '.join(unsaid)}; a run that says all three would."


def safe_score(
    step: str, case_id: str, facts: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """``score``, or None when it could not be worked out: the step goes on either way."""
    try:
        return score(step, case_id, facts)
    except Exception:  # noqa: BLE001 — a step is never stopped by its score
        logger.exception("could not score the %s on case %s", step, case_id)
        return None
