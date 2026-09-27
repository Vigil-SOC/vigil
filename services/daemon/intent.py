"""Observe-mode intent report: INTENT.md declared values beside the effective ones.

Run from a shell without a daemon:  python -m services.daemon.intent

``--replay`` re-decides recent findings and approval actions under the
declared manifest and the effective config, and prints the rows whose
outcome would differ. Read-only.

The daemon calls :func:`report_intent` once at startup. Lives in ``services``
because the effective values come from ``DaemonConfig.from_env()``, which
``core`` may not import.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select

from core.intent import (
    FIELDS_BY_KEY,
    IntentDiff,
    _normalize,
    diff_intent,
    effective_values,
    format_rows,
    intent_file,
    read_intent,
)
from core.response.approval_service import Reversibility
from core.response.config import (
    ResponseConfig,
    approval_requirement,
    response_action_decision,
)
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction, Finding
from core.time import utcnow
from services.daemon.config import DaemonConfig

logger = logging.getLogger(__name__)

FORCE_APPROVAL_PATH = "response.force_manual_approval"


def _overlay_force_manual_approval(
    effective: Dict[str, object], sources: Dict[str, str]
) -> None:
    """The one DB overlay ``from_env()`` does not apply.

    ``ApprovalService`` reads ``approval.force_manual_approval`` itself and the
    env flag forces it on, so the effective value is env OR db. Same
    try/except-and-continue as the orchestrator overlay: no DB, no overlay.
    """
    try:
        row = get_config_service().get_system_config("approval.force_manual_approval")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read approval config from DB: %s", exc)
        return
    if not row or not isinstance(row, dict) or not row.get("enabled"):
        return
    if not effective["respond.force_manual_approval"]:
        effective["respond.force_manual_approval"] = True
        sources[FORCE_APPROVAL_PATH] = "db"


def intent_report(
    config: Optional[DaemonConfig] = None,
    path: Optional[Path] = None,
    *,
    include_same: bool = False,
) -> Optional[List[IntentDiff]]:
    """Rows for declared keys, or ``None`` when the manifest is unreadable.

    Differing rows only, unless ``include_same`` (the Settings card).
    """
    declared = read_intent(path)
    if declared is None:
        return None
    config = config or DaemonConfig.from_env()
    effective = effective_values(config)
    sources = dict(config.sources)
    _overlay_force_manual_approval(effective, sources)
    return diff_intent(declared, effective, sources, include_same=include_same)


def report_intent(config: Optional[DaemonConfig] = None) -> None:
    """Log the report. Never raises: a broken manifest must not stop the daemon."""
    try:
        rows = intent_report(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intent report failed (non-fatal): %s", exc)
        return
    if rows is None:
        return
    if not rows:
        logger.info(
            "INTENT.md: no declared key differs from effective daemon config (%s)",
            intent_file(),
        )
        return
    for line in format_rows(rows):
        logger.info(line)


def effective_daemon_config(config: DaemonConfig) -> DaemonConfig:
    """Effective config with the live force-manual-approval OR applied.

    Env already lives on ``config``. The DB row is the other input, read by
    :func:`_overlay_force_manual_approval` rather than a second query.
    """
    values = effective_values(config)
    _overlay_force_manual_approval(values, {})
    if not values["respond.force_manual_approval"]:
        return config
    if config.response.force_manual_approval:
        return config
    return dataclasses.replace(
        config,
        response=dataclasses.replace(config.response, force_manual_approval=True),
    )


def declared_daemon_config(
    effective: DaemonConfig, declared: Mapping[str, Any]
) -> DaemonConfig:
    """Overlay declared intent onto a copy of the effective config.

    Paths come from ``FIELDS_BY_KEY``. A value of the wrong type is one
    warning and keeps the effective value, the same rule as ``diff_intent``.
    """
    current = effective_values(effective)
    grouped: Dict[str, Dict[str, Any]] = {}
    for key, want in declared.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None or key not in current:
            continue
        have = current[key]
        norm_want, norm_have = _normalize(want), _normalize(have)
        if norm_want is None or type(norm_want) is not type(norm_have):
            logger.warning(
                "INTENT.md key %r: declared %r is not a %s; ignored",
                key,
                want,
                type(have).__name__,
            )
            continue
        section, _, attr = field.path.partition(".")
        grouped.setdefault(section, {})[attr] = want
    config = effective
    for section, attrs in grouped.items():
        section_value = getattr(config, section)
        config = dataclasses.replace(
            config, **{section: dataclasses.replace(section_value, **attrs)}
        )
    return config


_SINCE = re.compile(r"^(\d+)([dhm])$")


def parse_since(text: str) -> timedelta:
    match = _SINCE.fullmatch(text.strip().lower())
    if not match:
        raise ValueError(
            f"invalid --since {text!r}; use Nd, Nh, or Nm (for example 7d)"
        )
    count = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=count)
    if unit == "h":
        return timedelta(hours=count)
    return timedelta(minutes=count)


@dataclass(frozen=True)
class ReplayFinding:
    id: str
    severity: str
    confidence: float
    recommended: str


@dataclass(frozen=True)
class ReplayApproval:
    id: str
    confidence: float
    reversibility: Reversibility


@dataclass(frozen=True)
class ReplayDiff:
    kind: str
    id: str
    effective: str
    declared: str
    effective_rule: str
    declared_rule: str


@dataclass(frozen=True)
class ReplayReport:
    rows: List[ReplayDiff]
    actions_now_requiring_approval: int
    actions_now_unattended: int
    findings_gained_or_lost: int


def replay_decisions(
    findings: Sequence[ReplayFinding],
    approvals: Sequence[ReplayApproval],
    effective: ResponseConfig,
    declared: ResponseConfig,
) -> ReplayReport:
    """Outcomes that differ between the two configs. No database access."""
    rows: List[ReplayDiff] = []
    now_require = 0
    now_unattended = 0
    gained_or_lost = 0
    for finding in findings:
        eff = response_action_decision(
            finding.severity, finding.confidence, finding.recommended, effective
        )
        dec = response_action_decision(
            finding.severity, finding.confidence, finding.recommended, declared
        )
        eff_action = eff[0] if eff else None
        dec_action = dec[0] if dec else None
        if eff_action == dec_action:
            continue
        if (eff_action is None) != (dec_action is None):
            gained_or_lost += 1
        rows.append(
            ReplayDiff(
                kind="finding",
                id=finding.id,
                effective=eff_action or "none",
                declared=dec_action or "none",
                effective_rule=eff[1] if eff else "",
                declared_rule=dec[1] if dec else "",
            )
        )
    for action in approvals:
        eff_req, eff_rule = approval_requirement(
            effective.force_manual_approval,
            action.reversibility,
            action.confidence,
            effective,
        )
        dec_req, dec_rule = approval_requirement(
            declared.force_manual_approval,
            action.reversibility,
            action.confidence,
            declared,
        )
        if eff_req == dec_req:
            continue
        if dec_req:
            now_require += 1
        else:
            now_unattended += 1
        rows.append(
            ReplayDiff(
                kind="action",
                id=action.id,
                effective="approval" if eff_req else "unattended",
                declared="approval" if dec_req else "unattended",
                effective_rule=eff_rule,
                declared_rule=dec_rule,
            )
        )
    return ReplayReport(
        rows=rows,
        actions_now_requiring_approval=now_require,
        actions_now_unattended=now_unattended,
        findings_gained_or_lost=gained_or_lost,
    )


def format_replay(report: ReplayReport) -> List[str]:
    lines = []
    for row in report.rows:
        effective_rule = f" {row.effective_rule}" if row.effective_rule else ""
        declared_rule = f" {row.declared_rule}" if row.declared_rule else ""
        lines.append(
            f"{row.kind} {row.id}: effective={row.effective}{effective_rule} "
            f"declared={row.declared}{declared_rule}"
        )
    findings = report.findings_gained_or_lost
    finding_noun = "finding" if findings == 1 else "findings"
    actions = report.actions_now_requiring_approval
    action_noun = "action" if actions == 1 else "actions"
    lines.append(
        "summary: "
        f"{actions} {action_noun} would have required approval that did not, "
        f"{report.actions_now_unattended} would have executed unattended that waited, "
        f"{findings} {finding_noun} would have gained or lost a response action"
    )
    return lines


def load_replay_rows(
    since: timedelta,
) -> tuple[List[ReplayFinding], List[ReplayApproval]]:
    """Findings and approval actions created inside the window."""
    cutoff = utcnow() - since
    findings: List[ReplayFinding] = []
    approvals: List[ReplayApproval] = []
    db = get_db_manager()
    if db._engine is None:
        db.initialize()
    with db.session_scope() as session:
        finding_rows = (
            session.execute(
                select(Finding)
                .where(Finding.created_at >= cutoff)
                .order_by(Finding.created_at.asc())
            )
            .scalars()
            .all()
        )
        for finding in finding_rows:
            enrichment = (
                finding.ai_enrichment if isinstance(finding.ai_enrichment, dict) else {}
            )
            if enrichment.get("triage_confidence") is None:
                continue
            try:
                confidence = float(enrichment["triage_confidence"])
            except (TypeError, ValueError):
                logger.warning(
                    "finding %s: triage_confidence %r is not a number; skipped",
                    finding.finding_id,
                    enrichment.get("triage_confidence"),
                )
                continue
            findings.append(
                ReplayFinding(
                    id=finding.finding_id,
                    severity=str(finding.severity or "medium").lower(),
                    confidence=confidence,
                    recommended=str(enrichment.get("recommended_action") or "").lower(),
                )
            )
        action_rows = (
            session.execute(
                select(ApprovalAction)
                .where(ApprovalAction.created_at >= cutoff)
                .order_by(ApprovalAction.created_at.asc())
            )
            .scalars()
            .all()
        )
        for action in action_rows:
            approvals.append(
                ReplayApproval(
                    id=action.action_id,
                    confidence=float(action.confidence),
                    reversibility=Reversibility(action.reversibility),
                )
            )
    return findings, approvals


def _report_main() -> int:
    rows = intent_report()
    path = intent_file()
    if rows is None:
        print(f"no intent report: {path} could not be read")
        return 0
    if not rows:
        print(f"{path}: no declared key differs from effective daemon config")
        return 0
    print(f"{path}: {len(rows)} key(s) differ from effective daemon config")
    for line in format_rows(rows):
        print(line)
    return 0


def _replay_main(since_text: str) -> int:
    try:
        since = parse_since(since_text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    declared = read_intent()
    path = intent_file()
    if declared is None:
        print(f"no replay: {path} could not be read")
        return 1
    effective = effective_daemon_config(DaemonConfig.from_env())
    proposed = declared_daemon_config(effective, declared)
    findings, approvals = load_replay_rows(since)
    report = replay_decisions(
        findings, approvals, effective.response, proposed.response
    )
    print(f"{path}: replay since {since_text}")
    for line in format_replay(report):
        print(line)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m services.daemon.intent")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="re-decide recent findings and approval actions under declared intent",
    )
    parser.add_argument(
        "--since",
        default="7d",
        help="replay window as Nd, Nh, or Nm (default 7d)",
    )
    args = parser.parse_args(argv)
    if args.replay:
        return _replay_main(args.since)
    return _report_main()


if __name__ == "__main__":
    sys.exit(main())
