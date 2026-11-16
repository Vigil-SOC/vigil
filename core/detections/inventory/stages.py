"""A rule's stage history, and a person's Revert, switch off and release of it.

The history is the rule's ``detection_rule_stages`` rows, newest first. A
person's Revert (live -> shadow) is a stage row with the person as actor and
``proof['hold']`` true: the rollout sweep does not promote a rule whose latest
row says so. A person's switch off (shadow -> off) is held the same way, so a
later version of the rule waits in shadow for a person instead of going live
on its own. A release is a later shadow row with ``hold`` false (the latest row with a ``hold`` key
decides, so a new version's row does not lift it), so the normal
rollout rules apply again, their shadow days counted from the release. Each
write is audited in the same transaction as the same ``detections.rule_staged``
entry Vigil's own moves write, the person as actor (the config audit log where
the trail is not in use).

A save that replaces earlier staged copies names them on its new version's stage
row (``proof['replaces']``); the history lists them per row, and lists, for the
rule read, the rules whose rows name it: its successors. A Sigma copy has no stage
rows, so its link is on the ``detections.rule_saved`` entry only, not read here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from core.audit import trail
from core.detections.inventory import readers
from core.detections.inventory.sync import staged_entry
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
        "replaces": (r.proof or {}).get("replaces") or [],
        "at": r.at.isoformat() if r.at else None,
    }


def _rows(session: Any, rule_id: int) -> List[Any]:
    """The rule's stage rows, newest first (by time, then id, as rollout and the
    listing order them), each with ``from_stage`` set."""
    rows = session.scalars(
        select(DetectionRuleStage)
        .where(DetectionRuleStage.rule_id == rule_id)
        .order_by(DetectionRuleStage.at.desc(), DetectionRuleStage.id.desc())
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
        .order_by(DetectionRuleStage.at.desc(), DetectionRuleStage.id.desc())
        .limit(1)
    ).all()
    return rule, rows[0] if rows else None


def _replaced_by(session: Any, rule_id: int) -> List[Dict[str, Any]]:
    """The rules saved to replace this one, each once, first saved first: those
    with a stage row naming it in ``proof['replaces']``."""
    rows = session.execute(
        select(
            DetectionRuleStage.rule_id,
            DetectionRuleStage.version,
            DetectionRuleStage.at,
            DetectionRule.name,
            DetectionRule.language,
        )
        .join(DetectionRule, DetectionRule.id == DetectionRuleStage.rule_id)
        .where(DetectionRuleStage.proof.contains({"replaces": [rule_id]}))
        .order_by(DetectionRuleStage.at, DetectionRuleStage.id)
    ).all()
    successors: Dict[int, Dict[str, Any]] = {}
    for rid, version, at, name, language in rows:
        successors.setdefault(
            rid,
            {
                "rule_id": rid,
                "name": name,
                "language": language,
                "version": version,
                "at": at.isoformat() if at else None,
            },
        )
    return list(successors.values())


def history(rule_id: int) -> Dict[str, Any]:
    """The rule's stage rows, newest first, whether a person's Revert holds it
    (the same hold rollout reads), and the rules saved to replace it."""
    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None:
            raise LookupError(f"No detection rule {rule_id}")
        return {
            "stages": [_row(r) for r in _rows(session, rule_id)],
            "held": _held(session, rule_id),
            "replaced_by": _replaced_by(session, rule_id),
        }


def _write(
    session: Any,
    rule: Any,
    latest: Any,
    stage: str,
    hold: bool,
    who: Tuple[str, str],
    reason: str,
    action: str,
) -> Dict[str, Any]:
    username, principal = who
    # Read before the add: a query after it would autoflush the new row.
    was_held = _held(session, rule.id)
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
    if trail.in_use(session):
        trail.append(
            session,
            staged_entry(
                principal,
                rule.id,
                rule.latest_version,
                latest.stage,
                stage,
                reason,
                hold,
                event=action,
                held_before=was_held,
                username=username,
            ),
        )
        return {"success": True, "rule_id": rule.id, "stage": stage, "hold": hold}
    ConfigService(user_id=principal)._create_audit_log(
        session,
        config_type=AUDIT_TYPE,
        config_key=f"detection_rule:{rule.id}",
        action="update",
        old_value={
            "rule_id": rule.id,
            "stage": latest.stage,
            "hold": was_held,
        },
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
        .order_by(DetectionRuleStage.at.desc(), DetectionRuleStage.id.desc())
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


def switch_off(rule_id: int, who: Tuple[str, str], reason: str) -> Dict[str, Any]:
    """shadow -> off by a person, held there. ``who`` is (username, principal id).

    The rollout sweep moves only shadow and live copies, and sync stages an
    unchanged copy again only when it comes back after removal, so the rule
    stays off. A new version still enters shadow, held: it runs but is not
    promoted until a person releases it. A promotion of it still waiting for a
    person is declined in the same transaction."""
    with get_db_manager().session_scope() as session:
        rule, latest = _context(rule_id, session)
        if latest.stage != "shadow":
            raise ValueError(f"Rule {rule_id} is {latest.stage}, not shadow")
        from core.detections.inventory.rollout_approval import withdraw_promotions

        result = _write(session, rule, latest, "off", True, who, reason, "switch_off")
        result["promotions_declined"] = withdraw_promotions(
            session, rule.id, who, reason
        )
        return result


def release(rule_id: int, who: Tuple[str, str], reason: str) -> Dict[str, Any]:
    """Lifts a person's hold, leaving the rule in the stage it is in (a held rule
    that is now off stays off); the rollout rules apply to it again."""
    with get_db_manager().session_scope() as session:
        rule, latest = _context(rule_id, session)
        if not _held(session, rule.id):
            raise ValueError(f"Rule {rule_id} is not held by a person")
        return _write(
            session, rule, latest, latest.stage, False, who, reason, "release"
        )
