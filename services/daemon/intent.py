"""Observe-mode intent report: INTENT.md declared values beside the effective ones.

Run from a shell without a daemon:  python -m services.daemon.intent

``--replay`` re-decides recent approval actions under the
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
from core.response.ceiling import environment_locks_act, try_read_stored_ceiling
from core.response.config import ResponseConfig, approval_requirement
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction
from core.time import utcnow
from services.daemon.config import CeilingConfig, DaemonConfig

logger = logging.getLogger(__name__)


def _overlay_ceiling(config: DaemonConfig) -> DaemonConfig:
    """Effective act flags from the ceiling row, forced off by the environment.

    A missing row is the seeded off state. A failed read stays off and does
    not report a grant. The file still does not enforce.
    """
    sources = dict(config.sources)
    loaded = try_read_stored_ceiling()
    if loaded is None:
        return dataclasses.replace(config, sources=sources)
    stored, found = loaded
    locked = environment_locks_act()
    origin = "db" if found else "default"
    sources["ceiling.act_reversible"] = (
        "env" if locked and stored.act_reversible else origin
    )
    sources["ceiling.act_irreversible"] = (
        "env" if locked and stored.act_irreversible else origin
    )
    sources["ceiling.four_eyes"] = origin
    ceiling = CeilingConfig(
        act_reversible=False if locked else stored.act_reversible,
        act_irreversible=False if locked else stored.act_irreversible,
        four_eyes=stored.four_eyes,
    )
    return dataclasses.replace(config, ceiling=ceiling, sources=sources)


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
    config = _overlay_ceiling(config or DaemonConfig.from_env())
    return diff_intent(
        declared,
        effective_values(config),
        config.sources,
        include_same=include_same,
    )


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
    """Effective config with the ceiling overlay applied.

    Env already lives on ``config``. The ceiling row is the other input.
    """
    return _overlay_ceiling(config)


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
class ReplayApproval:
    id: str
    confidence: float
    # The stored column, replayed as the class it names.
    reversibility: str


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


def replay_decisions(
    approvals: Sequence[ReplayApproval],
    effective: ResponseConfig,
    declared: ResponseConfig,
) -> ReplayReport:
    """Outcomes that differ between the two configs. No database access."""
    rows: List[ReplayDiff] = []
    now_require = 0
    now_unattended = 0
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
    actions = report.actions_now_requiring_approval
    action_noun = "action" if actions == 1 else "actions"
    lines.append(
        "summary: "
        f"{actions} {action_noun} would have required approval that did not, "
        f"{report.actions_now_unattended} would have executed unattended that waited"
    )
    return lines


def load_replay_rows(since: timedelta) -> List[ReplayApproval]:
    """Approval actions created inside the window."""
    cutoff = utcnow() - since
    approvals: List[ReplayApproval] = []
    db = get_db_manager()
    if db._engine is None:
        db.initialize()
    with db.session_scope() as session:
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
                    reversibility=action.reversibility,
                )
            )
    return approvals


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
    approvals = load_replay_rows(since)
    report = replay_decisions(approvals, effective.response, proposed.response)
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
        help="re-decide recent approval actions under declared intent",
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
