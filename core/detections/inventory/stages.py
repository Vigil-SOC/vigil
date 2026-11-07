"""A rule's stage history, and a person's Revert and release of it (#3996).

The history is the rule's ``detection_rule_stages`` rows, newest first. A
person's Revert (live -> shadow) is a stage row with the person as actor and
``proof['hold']`` true: the rollout sweep does not promote a rule whose latest
row says so. A release is a later shadow row with ``hold`` false (the latest row with a ``hold`` key
decides, so a new version's row does not lift it), so the normal
rollout rules apply again, their shadow days counted from the release. Each
write also lands in the config audit log, in the same transaction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from core.detections.inventory import readers
from core.storage.config_service import ConfigService
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleStage

AUDIT_TYPE = "detection_rules"


def _row(r: Any) -> Dict[str, Any]:
    return {
        "stage_id": r.id,
        "version": r.version,
        "from_stage": r.from_stage,
        "to_stage": r.stage,
        "actor": r.actor,
        "reason": r.reason,
        "at": r.at.isoformat() if r.at else None,
    }


def _rows(session: Any, rule_id: int) -> List[Any]:
    """The rule's stage rows, newest first, each with ``from_stage`` set."""
    rows = session.scalars(
        select(DetectionRuleStage)
        .where(DetectionRuleStage.rule_id == rule_id)
        .order_by(DetectionRuleStage.id.desc())
    ).all()
    for i, row in enumerate(rows):
        row.from_stage = rows[i + 1].stage if i + 1 < len(rows) else None
    return rows


def _state(session: Any, rule_id: int) -> Tuple[Optional[Any], Optional[Any]]:
    """(the rule, its latest stage row), either None."""
    rule = session.get(DetectionRule, rule_id)
    if rule is None or rule.source != readers.OVERLAY_SOURCE:
        return None, None
    rows = session.scalars(
        select(DetectionRuleStage)
        .where(DetectionRuleStage.rule_id == rule_id)
        .order_by(DetectionRuleStage.id.desc())
        .limit(1)
    ).all()
    return rule, rows[0] if rows else None


def history(rule_id: int) -> List[Dict[str, Any]]:
    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None:
            raise LookupError(f"No detection rule {rule_id}")
        return [_row(r) for r in _rows(session, rule_id)]


def _write(
    session: Any, rule: Any, latest: Any, stage: str, hold: bool, who: Tuple[str, str],
    reason: str, action: str,
) -> Dict[str, Any]:
    username, principal = who
    session.add(
        DetectionRuleStage(
            rule_id=rule.id,
            version=rule.latest_version,
            stage=stage,
            actor=username,
            reason=reason,
            proof={"hold": hold},
        )
    )
    ConfigService(user_id=principal)._create_audit_log(
        session,
        config_type=AUDIT_TYPE,
        config_key=f"detection_rule:{rule.id}",
        action="update",
        old_value={"rule_id": rule.id, "stage": latest.stage, "hold": _held(session, rule.id)},
        new_value={"rule_id": rule.id, "stage": stage, "hold": hold, "event": action},
        change_reason=reason,
    )
    return {"success": True, "rule_id": rule.id, "stage": stage, "hold": hold}


def _hold_row(session: Any, rule_id: int) -> Optional[Any]:
    """The latest row with a 'hold' key, whatever rows follow it."""
    return session.scalars(
        select(DetectionRuleStage)
        .where(
            DetectionRuleStage.rule_id == rule_id,
            DetectionRuleStage.proof.has_key("hold"),
        )
        .order_by(DetectionRuleStage.id.desc())
        .limit(1)
    ).first()


def _held(session: Any, rule_id: int) -> bool:
    row = _hold_row(session, rule_id)
    return row is not None and row.proof.get("hold") is True


def _context(rule_id: int, session: Any) -> Tuple[Any, Any]:
    rule, latest = _state(session, rule_id)
    if rule is None:
        raise LookupError(f"No detection rule {rule_id}")
    if latest is None:
        raise ValueError(f"Rule {rule_id} has no stage yet")
    return rule, latest


def revert(rule_id: int, who: Tuple[str, str], reason: str) -> Dict[str, Any]:
    """live -> shadow by a person, held there. ``who`` is (username, principal id)."""
    with get_db_manager().session_scope() as session:
        rule, latest = _context(rule_id, session)
        if latest.stage != "live":
            raise ValueError(f"Rule {rule_id} is {latest.stage}, not live")
        return _write(session, rule, latest, "shadow", True, who, reason, "revert")


def release(rule_id: int, who: Tuple[str, str], reason: str) -> Dict[str, Any]:
    """Lifts a person's hold: the rollout rules apply to the rule again."""
    with get_db_manager().session_scope() as session:
        rule, latest = _context(rule_id, session)
        if not _held(session, rule.id):
            raise ValueError(f"Rule {rule_id} is not held by a person")
        return _write(session, rule, latest, "shadow", False, who, reason, "release")
