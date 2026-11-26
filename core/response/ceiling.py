"""One autonomy ceiling (#2918).

The stored row is ``autonomy.ceiling``. Read is always on. Write of Vigil's
own records and propose are on unless ``stopped``. The act flags are off until a person
raises them. ``DAEMON_FORCE_APPROVAL`` and ``DAEMON_AUTO_RESPONSE`` can only
force the effective act flags off. Four-eyes is enforced where an approval
is recorded, in ``ApprovalService.approve_action`` (#2932).
An unbound caller is not denied here; the HTTP gates are the permission
checks on the routes, and ``/internal/tools/invoke`` refuses a tool call that
names nobody.

Each outside tool also has one stored effect class (#3304), read here by every
caller that decides whether a tool call needs an approval.

Each investigate-kind workflow may carry its own tier in the same row (#3624):
``act`` or ``assist`` replaces the fleet's ``act_reversible`` for its runs. A
person raises it with an agreement floor; Vigil drops it to ``assist`` when a
person's closure takes its agreement below that floor, or when the Wilson lower
bound on its sampled false closes sits above the complement of that floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import event, select, text

from core.config import Settings, get_settings
from core.response.config import (
    applied_intent,
    applied_values,
    managed_widening,
    settle_pending,
)
from core.storage import config_service as config_store
from core.storage.connection import get_db_session
from core.storage.models import (
    Finding,
    SystemConfig,
    ToolEffectClass,
    WorkflowRun,
    case_findings,
)
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

CEILING_KEY = "autonomy.ceiling"
ACT = "act"
ASSIST = "assist"
TIERS = (ACT, ASSIST)
AGREEMENT_WINDOW = timedelta(days=30)
RELEASED_PER_HOUR = 10
RELEASED_PER_HOUR_MAX = 1000


def _no_workflows() -> Mapping[str, dict]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Ceiling:
    act_reversible: bool = False
    act_irreversible: bool = False
    stopped: bool = False
    # Latest stop. Cleared on resume. The id is the principal's (a person's
    # or a service account's user id).
    stopped_by: Optional[str] = None
    stopped_at: Optional[str] = None
    four_eyes: bool = False
    # workflow id -> {tier, floor, evidence, by, at}. Absent follows the fleet.
    workflows: Mapping[str, dict] = field(default_factory=_no_workflows)
    # Declared crown jewels are left for a person (#5227). On unless stored off.
    hold_crown_jewels: bool = True
    # Proposals the ceiling releases in any 60 minutes (#5274). 0 releases none.
    released_per_hour: int = RELEASED_PER_HOUR


@dataclass(frozen=True)
class CeilingView:
    """What the console API returns.

    The act flags and ``four_eyes`` are the stored ceiling. Effective act is
    off while ``stopped`` or ``environment_locked``. Read stays on while
    stopped; write and propose follow ``stopped`` only.
    """

    act_reversible: bool
    act_irreversible: bool
    stopped: bool
    read: bool
    write: bool
    propose: bool
    environment_locked: bool
    four_eyes: bool


class CeilingRefusal(Exception):
    """A ceiling write the caller is not allowed to store."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CeilingWriteError(Exception):
    """The ceiling row was not stored."""


def _flag(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _on(value: Any) -> bool:
    """A missing or non-boolean value reads on: the reverse of ``_flag``."""
    return value if isinstance(value, bool) else True


def _cap(value: Any) -> int:
    """A whole number in 0..RELEASED_PER_HOUR_MAX; anything else reads as the default."""
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= RELEASED_PER_HOUR_MAX:
            return value
    return RELEASED_PER_HOUR


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def environment_locks_act(settings: Optional[Settings] = None) -> bool:
    """Force-approval, or auto-response turned off, beats a stored act flag.

    Neither variable can turn an act flag on. ``DAEMON_AUTO_RESPONSE`` defaults
    to true, which is not a grant. An applied INTENT.md ``respond.auto_response:
    false`` locks too; a declared true never clears the env lock.
    """
    settings = get_settings() if settings is None else settings
    return (
        bool(settings.daemon_force_approval)
        or not bool(settings.daemon_auto_response)
        or applied_values().get("respond.auto_response") is False
    )


def _workflow_entries(value: Any) -> Mapping[str, dict]:
    """Entries with a known tier; anything else follows the fleet."""
    if not isinstance(value, dict):
        return _no_workflows()
    return MappingProxyType(
        {
            str(wid): dict(entry)
            for wid, entry in value.items()
            if isinstance(entry, dict) and entry.get("tier") in TIERS
        }
    )


def _parse(value: Any) -> Ceiling:
    if not isinstance(value, dict):
        return Ceiling()
    return Ceiling(
        act_reversible=_flag(value.get("act_reversible")),
        act_irreversible=_flag(value.get("act_irreversible")),
        stopped=_flag(value.get("stopped")),
        stopped_by=_text(value.get("stopped_by")),
        stopped_at=_text(value.get("stopped_at")),
        four_eyes=_flag(value.get("four_eyes")),
        workflows=_workflow_entries(value.get("workflows")),
        hold_crown_jewels=_on(value.get("hold_crown_jewels")),
        released_per_hour=_cap(value.get("released_per_hour")),
    )


def read_stored_ceiling() -> tuple[Ceiling, bool]:
    """The stored row. ``(defaults, False)`` when the key is absent.

    Raises when the read fails. A missing row is the seeded off state, not Act.
    """
    value = config_store.get_config_service().read_system_config(CEILING_KEY)
    if value is None:
        return Ceiling(), False
    return _parse(value), True


def read_ceiling() -> Ceiling:
    stored, _found = read_stored_ceiling()
    return stored


def try_read_stored_ceiling() -> Optional[tuple[Ceiling, bool]]:
    """The stored row, or None when the read fails (act flags stay off)."""
    try:
        return read_stored_ceiling()
    except Exception as exc:  # noqa: BLE001
        logger.error("autonomy ceiling not read; act flags stay off: %s", exc)
        return None


def workflow_tier(stored: Ceiling, workflow_id: Optional[str]) -> Optional[str]:
    entry = stored.workflows.get(workflow_id) if workflow_id else None
    return entry.get("tier") if entry else None


def stored_act(
    stored: Ceiling, effect: Optional[str], workflow_id: Optional[str]
) -> bool:
    """Whether the stored row lets a run of ``workflow_id`` take ``effect``.

    Stop and the environment lock are not read here. A workflow's tier replaces
    the fleet's ``act_reversible``; ``irreversible`` always needs the fleet's
    ``act_irreversible`` and never runs under ``assist``.
    """
    tier = workflow_tier(stored, workflow_id)
    if effect == REVERSIBLE:
        return stored.act_reversible if tier is None else tier == ACT
    if effect == IRREVERSIBLE:
        return stored.act_irreversible and tier != ASSIST
    return False


def effective_act(
    stored: Ceiling,
    effect: Optional[str],
    workflow_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """The one answer to "may this run act": stop and the lock beat every tier."""
    if stored.stopped or environment_locks_act(settings):
        return False
    return stored_act(stored, effect, workflow_id)


def workflow_of(run_id: Optional[str]) -> Optional[str]:
    """The workflow behind a run, or None (no workflow: the fleet applies).

    Raises when the read fails: an unknown workflow may be on Assist, so the
    caller holds rather than falling back to the fleet flags.
    """
    if not run_id:
        return None
    with unit_of_work() as session:
        return session.execute(
            select(WorkflowRun.workflow_id).where(WorkflowRun.run_id == run_id)
        ).scalar()


def _workflow_held(run_id: Optional[str]) -> tuple[Optional[str], bool]:
    """``(workflow, read)``; ``read`` is False when the lookup failed."""
    try:
        return workflow_of(run_id), True
    except Exception as exc:  # noqa: BLE001 — no answer is not permission
        logger.error("workflow of run %s not read; act held: %s", run_id, exc)
        return None, False


Values = Iterable[str] | Callable[[], Iterable[str]]


def crown_jewel_held(
    values: Values, stored: Ceiling, *, exact: bool = False
) -> Optional[str]:
    """Why an act naming a declared crown jewel is left for a person, or None (#5227).

    ``values`` are Entity Keys or argument values, or a callable read only once
    a site is declared. None while the hold is off or no site is declared. A
    site that has never been read, or values that can't be read, hold the act.
    """
    if not stored.hold_crown_jewels:
        return None
    from core import site

    try:
        row = site.applied_site(strict=True)
        if not site.declared(row):
            return None
        jewel = site.crown_jewel(
            values() if callable(values) else values, row, exact=exact
        )
    except Exception as exc:  # noqa: BLE001 — no answer is not permission
        logger.error("crown jewels not checked; act held: %s", exc)
        return f"declared crown jewels could not be checked ({type(exc).__name__})"
    if jewel is None:
        return None
    return (
        f"{jewel['name']} is a declared crown jewel "
        f"({jewel.get('file')}:{jewel.get('line')}, revision {jewel.get('revision')})"
    )


def finding_keys(
    *, case_id: Optional[str] = None, finding_ids: Iterable[str] = ()
) -> list[str]:
    """The Entity Keys of a case's findings, or of the findings ``finding_ids`` name."""
    from core.memory.entity_keys import finding_entity_keys

    query = select(Finding.entity_context)
    if case_id:
        query = query.join(
            case_findings, case_findings.c.finding_id == Finding.finding_id
        ).where(case_findings.c.case_id == case_id)
    else:
        ids = [str(i) for i in finding_ids if i]
        if not ids:
            return []
        query = query.where(Finding.finding_id.in_(ids))
    with unit_of_work() as session:
        contexts = session.execute(query).scalars().all()
    return finding_entity_keys([{"entity_context": c} for c in contexts])


CLOSE_HELD = "autonomy does not let a model close cases on its own"


def close_held(run_id: Optional[str], keys: Values = ()) -> Optional[str]:
    """Why a model's close of a case is held, or None when it may close (#4819).

    Closing is a reversible act: it passes only where the run's workflow, or
    with no workflow (chat, an /mcp caller) the fleet, may act on its own, and
    then only when the case's ``keys`` name no declared crown jewel (#5227).
    A ceiling that can't be read holds the close.
    """
    try:
        stored = read_ceiling()
        if effective_act(stored, REVERSIBLE, workflow_of(run_id)):
            # No keys (the production report's status read) never reads the site.
            return crown_jewel_held(keys, stored) if keys else None
        return CLOSE_HELD
    except Exception as exc:  # noqa: BLE001 — no answer is not permission
        logger.exception("autonomy not read; close for run %s held", run_id)
        return f"the autonomy setting could not be read ({type(exc).__name__})"


def ceiling_view(stored: Ceiling, settings: Optional[Settings] = None) -> CeilingView:
    return CeilingView(
        act_reversible=stored.act_reversible,
        act_irreversible=stored.act_irreversible,
        stopped=stored.stopped,
        read=True,
        write=not stored.stopped,
        propose=not stored.stopped,
        environment_locked=environment_locks_act(settings),
        four_eyes=stored.four_eyes,
    )


def _labels(ids: set) -> dict[str, str]:
    """Principal id to its label; blank or erased labels are left out."""
    if not ids:
        return {}
    with get_db_session() as session:
        rows = session.execute(
            text(
                "SELECT principal_id, label FROM public.principals "
                "WHERE principal_id = ANY(:ids)"
            ),
            {"ids": list(ids)},
        ).all()
    return {pid: label.strip() for pid, label in rows if label and label.strip()}


def ceiling_read() -> dict[str, Any]:
    """The ceiling as ``GET /api/v1/autonomy`` and the MCP tool return it.

    Each workflow entry gains ``by_label``, the principal's name read now (#3642).
    A stop names ``stopped_by`` the same way. ``held`` is the approved outside
    actions that stop is still holding.
    """
    stored = read_ceiling()
    named = {
        e.get("by")
        for e in stored.workflows.values()
        if e.get("by") not in (None, "system")
    }
    if stored.stopped_by not in (None, "system"):
        named.add(stored.stopped_by)
    labels = _labels(named)
    # approval_service imports this module; a top-level import cycles.
    from core.response.approval_service import held_for_ceiling

    return {
        **vars(ceiling_view(stored)),
        "stopped_by": stored.stopped_by,
        "stopped_at": stored.stopped_at,
        "stopped_by_label": (
            labels.get(stored.stopped_by) if stored.stopped_by else None
        ),
        "workflows": {
            wid: {**entry, "by_label": labels.get(entry.get("by"))}
            for wid, entry in stored.workflows.items()
        },
        "held": held_for_ceiling(),
        "hold_crown_jewels": stored.hold_crown_jewels,
        "crown_jewels": _crown_jewel_count(),
        "released_per_hour": stored.released_per_hour,
    }


def _crown_jewel_count() -> int:
    """Crown jewels in the applied site revision; 0 when none or unreadable."""
    from core import site

    try:
        row = site.applied_site()
        if not site.declared(row):
            return 0
        return len((row.get("items") or {}).get("crown_jewels") or [])
    except Exception as exc:  # noqa: BLE001 — a count, never a refusal
        logger.warning("crown jewels not counted: %s", exc)
        return 0


def execution_held() -> bool:
    """No outside action runs while stopped, or while the ceiling cannot be read."""
    try:
        stored, _found = read_stored_ceiling()
    except Exception as exc:  # noqa: BLE001
        logger.error("autonomy ceiling not read; holding execution: %s", exc)
        return True
    return stored.stopped


def _dump(ceiling: Ceiling) -> dict[str, Any]:
    return {
        "act_reversible": ceiling.act_reversible,
        "act_irreversible": ceiling.act_irreversible,
        "stopped": ceiling.stopped,
        "stopped_by": ceiling.stopped_by,
        "stopped_at": ceiling.stopped_at,
        "four_eyes": ceiling.four_eyes,
        "workflows": {wid: dict(e) for wid, e in ceiling.workflows.items()},
        "hold_crown_jewels": ceiling.hold_crown_jewels,
        "released_per_hour": ceiling.released_per_hour,
    }


def _raises(current: bool, requested: Optional[bool]) -> bool:
    return requested is True and not current


def raises_cap(current: int, requested: Optional[int]) -> bool:
    return requested is not None and requested > current


def update_ceiling(
    *,
    user_id: str,
    caller_is_service: bool,
    act_reversible: Optional[bool] = None,
    act_irreversible: Optional[bool] = None,
    four_eyes: Optional[bool] = None,
    hold_crown_jewels: Optional[bool] = None,
    released_per_hour: Optional[int] = None,
    lowers_only: bool = False,
    locked: Optional[bool] = None,
) -> Ceiling:
    """Store a person's edit of the ceiling. Does not execute anything.

    A service account may lower an act flag. It may not raise one, resume,
    switch four-eyes, turn the crown-jewel hold off, or raise the hourly cap. An act flag is not stored on while the environment
    has locked them off. While stopped, raising an act flag or the hourly cap, or turning
    four-eyes or the crown-jewel hold off is refused; a lower, and turning
    either on, still store.
    The refusal is decided on the locked row, so a stop that lands during
    the write is not stored past.
    """
    if locked is None:
        locked = environment_locks_act()
    applied = applied_intent(fresh=True)
    asked = {
        key: value
        for key, value in (
            ("act.reversible", act_reversible),
            ("act.irreversible", act_irreversible),
            ("act.four_eyes", four_eyes),
            ("act.hold_crown_jewels", hold_crown_jewels),
        )
        if value is not None
    }

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        # A lower-only caller whose number is above a cap lowered since it read
        # leaves the cap as it is; the rest of its edit still stores.
        cap = (
            None
            if lowers_only and raises_cap(current.released_per_hour, released_per_hour)
            else released_per_hour
        )
        wants_act_on = act_reversible is True or act_irreversible is True
        if locked and wants_act_on:
            raise CeilingRefusal(409, "The environment wins; Act was not saved.")
        # A declared directory's act.* keys may be narrowed here, never widened.
        for key, value in asked.items():
            refused = managed_widening(key, value, applied)
            if refused:
                raise CeilingRefusal(409, refused)
        if caller_is_service:
            if four_eyes is not None and four_eyes != current.four_eyes:
                raise CeilingRefusal(403, "A service account cannot switch four-eyes")
            if hold_crown_jewels is False and current.hold_crown_jewels:
                raise CeilingRefusal(
                    403, "A service account cannot turn the crown-jewel hold off"
                )
            if (
                _raises(current.act_reversible, act_reversible)
                or _raises(current.act_irreversible, act_irreversible)
                or raises_cap(current.released_per_hour, cap)
            ):
                raise CeilingRefusal(403, "A service account cannot raise the ceiling")
        if current.stopped and (
            _raises(current.act_reversible, act_reversible)
            or _raises(current.act_irreversible, act_irreversible)
            or (four_eyes is False and current.four_eyes)
            or (hold_crown_jewels is False and current.hold_crown_jewels)
            or raises_cap(current.released_per_hour, cap)
        ):
            raise CeilingRefusal(409, "Stopped: resume first")
        edit = {
            name: value
            for name, value in (
                ("act_reversible", act_reversible),
                ("act_irreversible", act_irreversible),
                ("four_eyes", four_eyes),
                ("hold_crown_jewels", hold_crown_jewels),
                ("released_per_hour", cap),
            )
            if value is not None
        }
        return replace(current, **edit), "Autonomy ceiling updated"

    stored = _locked_update(change, user_id)
    settle_pending(asked, user_id)
    return stored


def stop_ceiling(user_id: str) -> Ceiling:
    """Set ``stopped`` and record who stopped it, and when.

    Stored act flags stay, so a later resume restores them. A stop while
    already stopped keeps that first snapshot, updates who stopped it, and
    writes its own entry. Approved outside actions the executor runs are
    marked held in this same transaction. This does not execute anything.
    """

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        return (
            replace(
                current,
                stopped=True,
                stopped_by=user_id,
                stopped_at=utcnow().isoformat(),
            ),
            "Autonomy stopped",
        )

    def hold(session: Any) -> None:
        # approval_service imports this module; a top-level import cycles.
        from core.response.approval_service import stamp_held_approvals

        stamp_held_approvals(session)

    # A stop only tightens, so it lands during a restore's write pause too.
    return _locked_update(change, user_id, after=hold, gated=False)


def resume_ceiling(*, user_id: str, caller_is_service: bool) -> Ceiling:
    """Clear ``stopped``. Act flags already lowered during the stop stay off.

    Held actions stay held. Executes nothing. A service account cannot resume.
    """
    if caller_is_service:
        raise CeilingRefusal(403, "A service account cannot resume")

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        if not current.stopped:
            return current, "Autonomy resumed"
        return (
            replace(current, stopped=False, stopped_by=None, stopped_at=None),
            "Autonomy resumed",
        )

    return _locked_update(change, user_id)


def locked_flags(session: Any) -> tuple[bool, bool]:
    """``(stopped, four_eyes)`` with the ceiling row share-locked until commit.

    Call this before locking an approval row. Stop locks the ceiling first
    and then the approval rows; the other order deadlocks.
    """
    row = session.execute(
        select(SystemConfig)
        .where(SystemConfig.key == CEILING_KEY)
        .with_for_update(read=True)
    ).scalar_one_or_none()
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return value.get("stopped") is True, value.get("four_eyes") is True


def raises_workflow(current: Ceiling, workflow_id: str, after: Ceiling) -> bool:
    """A raise is anything that lets the workflow act where it could not.

    Stop and the environment lock are ignored: they hold every tier alike.
    """
    return any(
        stored_act(after, effect, workflow_id)
        and not stored_act(current, effect, workflow_id)
        for effect in (REVERSIBLE, IRREVERSIBLE)
    )


def _evidence(tally: Optional[dict], window_end: str) -> dict[str, Any]:
    tally = tally or {}
    return {
        "agreed": tally.get("agreed", 0),
        "disagreed": tally.get("disagreed", 0),
        "agreement_rate": tally.get("agreement_rate"),
        "window_end": window_end,
        "false_close_rate": tally.get("false_close_rate"),
        "answered": tally.get("answered", 0),
        "wilson_low": tally.get("wilson_low"),
        "wilson_high": tally.get("wilson_high"),
    }


def _false_close_above(tally: dict, floor: int) -> bool:
    """Act drops when the sample is large enough and its Wilson low is too high.

    At least 20 draws answered, at least half the draws answered, and the
    Wilson lower bound strictly above the complement of the floor.
    """
    answered = tally.get("answered") or 0
    draws = tally.get("draws") or 0
    low = tally.get("wilson_low")
    if answered < 20 or draws < 1 or answered * 2 < draws:
        return False
    if not isinstance(low, (int, float)):
        return False
    # Same 4 decimals as the stored bound. ``1 - floor / 100`` is a hair under
    # the complement for many floors, which would treat a bound that landed on
    # the complement as strictly above it.
    return low > round((100 - floor) / 100, 4)


def agreement_by_workflow(session) -> tuple[dict[str, dict], str]:
    """Each workflow's 30-day agreement tally, with its ``by_model`` split, and
    the window's end."""
    from core.reporting.analytics_service import agent_agreement

    end = utcnow()
    scored = agent_agreement(session, end - AGREEMENT_WINDOW, end)
    tallies = {t["workflow_id"]: t for t in scored["by_workflow"]}
    return tallies, end.isoformat()


def model_tally(tally: Optional[dict], model: Optional[str]) -> Optional[dict]:
    """The workflow's tally under ``model``; the blended one when no model is named.

    None when the model has no record on the workflow."""
    if not model:
        return tally
    return next(
        (m for m in (tally or {}).get("by_model", ()) if m.get("model") == model),
        None,
    )


def investigation_model() -> Optional[str]:
    """The model an investigation is handed now, or None when it cannot be resolved."""
    from core.llm import target

    try:
        resolved = target.resolve_component("investigation")
    except Exception:  # noqa: BLE001 - unresolved is judged on the blended tally
        logger.exception("investigation model not resolved")
        return None
    return resolved[1] if resolved else None


def workflow_evidence(workflow_id: str, model: Optional[str] = None) -> dict[str, Any]:
    """The workflow's 30-day agreement under ``model`` (blended when None),
    computed here and never taken from a client."""
    with unit_of_work() as session:
        tallies, window_end = agreement_by_workflow(session)
    return _evidence(model_tally(tallies.get(workflow_id), model), window_end)


def workflow_entry(
    tier: str,
    floor: Optional[int],
    evidence: Optional[dict],
    by: str,
    model: Optional[str] = None,
) -> dict[str, Any]:
    entry = {
        "tier": tier,
        "floor": floor,
        "evidence": evidence,
        "by": by,
        "at": utcnow().isoformat(),
    }
    # The model the tier was earned (or lost) on; absent when it was not resolved.
    if model:
        entry["model"] = model
    return entry


def _locked_update(
    change: Callable[[Ceiling], tuple[Ceiling, str]],
    actor: str,
    after: Optional[Callable[[Any], None]] = None,
    *,
    gated: bool = True,
) -> Ceiling:
    """Read, change and write the row in one transaction with it locked.

    ``change`` sees the row as it is now, so no other write is lost, and the
    write leaves the same ``config.change`` entry as ``set_system_config``.
    ``after`` runs in that transaction, before the write is skipped as a no-op.
    ``gated=False`` is for a change that only lowers autonomy.
    """
    try:
        return _write_locked(change, actor, after, gated)
    except CeilingRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CeilingWriteError("autonomy.ceiling was not stored") from exc


def _write_locked(
    change: Callable[[Ceiling], tuple[Ceiling, str]],
    actor: str,
    after: Optional[Callable[[Any], None]] = None,
    gated: bool = True,
):
    with unit_of_work() as session:
        row = session.execute(
            select(SystemConfig)
            .where(SystemConfig.key == CEILING_KEY)
            .with_for_update()
        ).scalar_one_or_none()
        current = _parse(row.value) if row is not None else Ceiling()
        updated, reason = change(current)
        if after is not None:
            after(session)
        if updated == current:
            return current
        old = row.value if row is not None else None
        value = _dump(updated)
        if row is None:
            session.add(
                SystemConfig(
                    key=CEILING_KEY,
                    value=value,
                    description="Autonomy ceiling",
                    config_type="autonomy",
                    updated_by=actor,
                )
            )
        else:
            row.value = value
            row.updated_by = actor
        config_store.ConfigService(user_id=actor)._create_audit_log(
            session,
            config_type="autonomy",
            config_key=CEILING_KEY,
            action="create" if old is None else "update",
            old_value=old,
            new_value=value,
            change_reason=reason,
            gated=gated,
        )
        return updated


def set_workflow_tier(
    workflow_id: str, entry: Optional[dict], user_id: str, *, raises: bool
) -> Ceiling:
    """Store one workflow's entry; None returns it to the fleet flags.

    A raise is refused if a stop landed since the caller looked.
    """

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        if raises and current.stopped:
            raise CeilingRefusal(409, "Stopped: resume first")
        workflows = dict(current.workflows)
        if entry is None:
            workflows.pop(workflow_id, None)
        else:
            workflows[workflow_id] = entry
        updated = replace(current, workflows=MappingProxyType(workflows))
        return updated, f"Autonomy of workflow {workflow_id} set"

    return _locked_update(change, user_id)


def drop_below_floor() -> None:
    """Drop Act workflows that missed a floor or whose false-close interval is too high.

    Each entry is judged on the record of the model it names. An Act entry that
    names none (written before #5222) is stamped with the investigation model in
    force and judged on that model's record; when that cannot be resolved it
    stays unstamped and is judged on the workflow's blended tally.

    Written as the system, under the row lock, so a concurrent edit is not
    lost. Never raises. A person's raise is still ``put_workflow_autonomy``.
    """
    try:
        with unit_of_work() as session:
            tallies, window_end = agreement_by_workflow(session)
        # Resolved only when an Act entry still lacks a model, and outside the lock.
        read = try_read_stored_ceiling()
        unstamped = read is None or any(
            e.get("tier") == ACT and not e.get("model")
            for e in read[0].workflows.values()
        )
        in_force = investigation_model() if unstamped else None
        below: list[str] = []
        false_close: list[str] = []
        stamped: list[str] = []

        def change(current: Ceiling) -> tuple[Ceiling, str]:
            workflows = dict(current.workflows)
            for wid, entry in current.workflows.items():
                floor = entry.get("floor")
                if entry.get("tier") != ACT or not isinstance(floor, int):
                    continue
                model = entry.get("model")
                if not model and in_force:
                    model = in_force
                    workflows[wid] = {**entry, "model": model}
                    stamped.append(wid)
                tally = model_tally(tallies.get(wid), model) or {}
                evidence = _evidence(tally, window_end)
                rate = evidence["agreement_rate"]
                missed = rate is not None and rate < floor / 100
                sampled = _false_close_above(tally, floor)
                if not missed and not sampled:
                    continue
                workflows[wid] = workflow_entry(ASSIST, floor, evidence, _SYSTEM, model)
                if missed:
                    below.append(wid)
                if sampled:
                    false_close.append(wid)
            parts: list[str] = []
            if below:
                parts.append(
                    "Agreement fell below the floor: " + ", ".join(sorted(below))
                )
            if false_close:
                parts.append(
                    "False-close interval above the complement of the floor: "
                    + ", ".join(sorted(false_close))
                )
            if stamped:
                parts.append(
                    "Model stamped on Act workflows: " + ", ".join(sorted(stamped))
                )
            reason = "; ".join(parts) if parts else "Agreement fell below the floor"
            return replace(current, workflows=MappingProxyType(workflows)), reason

        # A demotion only lowers autonomy: not held by a restore's write pause.
        _locked_update(change, _SYSTEM, gated=False)
        moved = sorted(set(below) | set(false_close))
        if moved:
            logger.warning("workflows dropped to Assist: %s", ", ".join(moved))
    except Exception:  # noqa: BLE001 - a close never fails on this
        logger.exception("agreement floors not checked after a close")


_DROP_PENDING = "autonomy.drop_pending"


def _after_commit(session) -> None:
    # A SAVEPOINT release fires this too; only the outermost commit counts.
    if session.in_nested_transaction():
        return
    if session.info.pop(_DROP_PENDING, False):
        drop_below_floor()


def _after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_DROP_PENDING, None)


def drop_after_commit(session) -> None:
    """Check the floors once ``session`` commits; a rolled-back close drops nothing."""
    session.info[_DROP_PENDING] = True
    if not event.contains(session, "after_commit", _after_commit):
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_rollback", _after_rollback)


class ModelJudgeError(Exception):
    """A model change could not be judged. A run handed that model does not start."""


_SCORED = 20  # the sample the false-close rule and the run judge use


def _latest_replay(session) -> Optional[dict]:
    """The stored replay when it succeeded within the agreement window, else None."""
    from core.storage.models import InvestigationReplay

    row = session.get(InvestigationReplay, 1)
    if row is None or row.status != "succeeded":
        return None
    if row.started_at < utcnow() - AGREEMENT_WINDOW:
        return None
    return {
        "model": row.model_id,
        "started_at": row.started_at.isoformat(),
        "by_workflow": row.by_workflow or {},
    }


def _short(tally: Optional[dict], floor: int) -> Optional[str]:
    """Why ``tally`` does not hold Act at ``floor``; None when it does."""
    if not tally:
        return "no record"
    scored = (tally.get("agreed") or 0) + (tally.get("disagreed") or 0)
    rate = tally.get("agreement_rate")
    if scored < _SCORED:
        return f"{scored} scored closes, under {_SCORED}"
    if not isinstance(rate, (int, float)) or rate < floor / 100:
        return f"agreement {rate} below the floor {floor}%"
    if _false_close_above(tally, floor):
        return "false-close interval above the complement of the floor"
    return None


def _verdict(
    wid: str,
    floor: Any,
    candidate: str,
    tallies: dict,
    replay: Optional[dict],
    window_end: str,
) -> tuple[bool, dict, str]:
    """``(keeps, evidence, why)``: the candidate's live record, else its latest replay."""
    live = model_tally(tallies.get(wid), candidate)
    live_ev = {**_evidence(live, window_end), "source": "live"}
    if not isinstance(floor, int):
        return False, live_ev, "no floor"
    why_live = _short(live, floor)
    if why_live is None:
        return True, live_ev, "live record"
    part = (replay or {}).get("by_workflow", {}).get(wid)
    if replay is None or replay["model"] != candidate:
        why_replay = "no replay in the window"
    elif not part:
        why_replay = "replay holds no closes of this workflow"
    else:
        # Replayed stands for answered, as _false_close_above reads the live sample.
        tally = {
            "agreed": part.get("agreed"),
            "disagreed": part.get("disagreed"),
            "agreement_rate": part.get("agreement_rate"),
            "false_close_rate": part.get("false_close_rate"),
            "answered": part.get("replayed"),
            "draws": part.get("total"),
            "wilson_low": part.get("false_close_wilson_low"),
            "wilson_high": part.get("false_close_wilson_high"),
        }
        why_replay = _short(tally, floor)
        if why_replay is None:
            ev = {
                **_evidence(tally, replay["started_at"]),
                "source": "replay",
                "replay_started_at": replay["started_at"],
            }
            return True, ev, "replay"
    return False, live_ev, f"live: {why_live}; replay: {why_replay}"


def _judge(
    workflows: Mapping[str, dict],
    candidate: Optional[str],
    stamp: Optional[str],
    only: Optional[str],
    tallies: Optional[dict],
    replay: Optional[dict],
    window_end: Optional[str],
) -> tuple[dict[str, dict], list[dict], str]:
    """The entries after judging, one result per Act workflow, and the audit reason.

    An unstamped Act entry takes ``stamp`` first. Never raises a workflow.
    """
    out = dict(workflows)
    results: list[dict] = []
    for wid, entry in workflows.items():
        if (only is not None and wid != only) or entry.get("tier") != ACT:
            continue
        model = entry.get("model")
        result = {"workflow_id": wid, "from_model": model, "to_model": candidate}
        if not model:
            if not stamp:
                results.append(
                    {
                        **result,
                        "outcome": "unstamped",
                        "reason": "Names no model and none to stamp",
                    }
                )
                continue
            model = stamp
            out[wid] = {**entry, "model": model}
            result.update(
                from_model=model, outcome="stamped", reason=f"Stamped with {model}"
            )
            if model == candidate:
                results.append(result)
                continue
        if model == candidate:
            results.append(
                {**result, "outcome": "unchanged", "reason": "Model unchanged"}
            )
            continue
        if candidate is None:
            raise ModelJudgeError(
                f"{wid} is on Act under {model} and the investigation model was not resolved"
            )
        if tallies is None:
            raise ModelJudgeError("the agreement record was not read")
        floor = entry.get("floor")
        keeps, evidence, why = _verdict(
            wid, floor, candidate, tallies, replay, window_end
        )
        if keeps:
            reason = f"Model changed from {model} to {candidate}: kept Act on {candidate}'s {why}"
            out[wid] = {
                **out[wid],
                "model": candidate,
                "evidence": evidence,
                "reason": reason,
            }
        else:
            reason = (
                f"Model changed from {model} to {candidate}: dropped to Assist ({why})"
            )
            out[wid] = {
                **workflow_entry(ASSIST, floor, evidence, _SYSTEM, candidate),
                "from_model": model,
                "reason": reason,
            }
        results.append(
            {
                **result,
                "from_model": model,
                "outcome": "keep" if keeps else "drop",
                "source": evidence["source"] if keeps else None,
                "reason": reason,
                "evidence": evidence,
            }
        )
    audit = "; ".join(
        f"{r['workflow_id']}: {r['reason']}"
        for r in results
        if r["outcome"] != "unchanged"
    )
    return out, results, audit or "Model change judged"


def judge_model_change(
    candidate: Optional[str],
    *,
    stamp: Optional[str] = None,
    only: Optional[str] = None,
    write: bool = False,
) -> list[dict]:
    """Judge each Act workflow (or ``only`` that one) whose model is not ``candidate``.

    It keeps Act where the candidate's live record, or its latest replay, for
    that workflow holds the floor on at least 20 scored closes, and otherwise
    drops to Assist by the system with a reason naming both models. An Act entry
    with no model takes ``stamp`` first. ``write=False`` is the preview: the same
    judgement, nothing stored. Raises ModelJudgeError when it cannot be made.
    """
    read = try_read_stored_ceiling()
    if read is None:
        raise ModelJudgeError("Could not read the autonomy ceiling")

    def judged(current: Ceiling) -> list[tuple[str, dict]]:
        return [
            (wid, e)
            for wid, e in current.workflows.items()
            if (only is None or wid == only)
            and e.get("tier") == ACT
            and (e.get("model") or stamp) != candidate
            and (e.get("model") or stamp)
        ]

    pending = judged(read[0])
    tallies = replay = window_end = None
    if pending:
        if candidate is None:
            raise ModelJudgeError(
                "The investigation model was not resolved, and Act workflows name "
                + ", ".join(sorted({e.get("model") or stamp for _, e in pending}))
            )
        try:
            with unit_of_work() as session:
                tallies, window_end = agreement_by_workflow(session)
                replay = _latest_replay(session)
        except Exception as exc:  # noqa: BLE001
            raise ModelJudgeError("Could not read the agreement record") from exc
    workflows, results, _ = _judge(
        read[0].workflows, candidate, stamp, only, tallies, replay, window_end
    )
    if not write or workflows == dict(read[0].workflows):
        return results

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        # Judged again on the locked row, so a concurrent edit is not lost.
        workflows, fresh, reason = _judge(
            current.workflows, candidate, stamp, only, tallies, replay, window_end
        )
        results[:] = fresh
        return replace(current, workflows=MappingProxyType(workflows)), reason

    try:
        # Only lowers or restamps: not held by a restore's write pause.
        _locked_update(change, _SYSTEM, gated=False)
    except CeilingWriteError as exc:
        raise ModelJudgeError("The judgement was not stored") from exc
    dropped = [r["workflow_id"] for r in results if r["outcome"] == "drop"]
    if dropped:
        logger.warning(
            "model change dropped workflows to Assist: %s", ", ".join(dropped)
        )
    return results


def preview_model_change(component: str, provider_id: str, model_id: str) -> dict:
    """What saving ``component`` as ``provider_id``/``model_id`` would do to Act
    workflows: the investigation model before and after, and each Act workflow's
    outcome. Read only, through the judgement the save runs.

    ValueError when the save itself would be refused (unknown component, or a
    provider that is missing or inactive); ModelJudgeError when it cannot be judged.
    """
    from core.llm import target
    from core.llm.providers.registry import (
        FALLBACK_KEY,
        ComponentAssignment,
        get_registry,
        is_valid_component,
    )
    from core.llm.target import blank_model
    from core.storage.models import LLMProviderConfig

    if not is_valid_component(component):
        raise ValueError(f"unknown component: {component}")
    with unit_of_work() as session:
        provider = session.get(LLMProviderConfig, provider_id)
        if provider is None or not provider.is_active:
            raise ValueError(f"provider {provider_id} is missing or not active")

    assignments = dict(get_registry().get_all_assignments())
    stored = assignments.get(component)
    settings: dict = {}
    # The save keeps a stored fallback on the same provider, unless it is the model.
    fallback = (
        blank_model((stored.settings or {}).get(FALLBACK_KEY)) if stored else None
    )
    if (
        stored
        and stored.provider_id == provider_id
        and fallback
        and fallback != model_id
    ):
        settings[FALLBACK_KEY] = fallback
    assignments[component] = ComponentAssignment(
        component, provider_id, model_id, settings
    )
    before = investigation_model()
    after = target.resolve_component("investigation", assignments)
    candidate = after[1] if after else None
    moves = _moves(before, candidate)
    return {
        "component": component,
        "provider_id": provider_id,
        "model_id": model_id,
        "investigation_model_before": before,
        "investigation_model_after": candidate,
        "moves": moves,
        # The save judges only a move; anything else is left to the handoff.
        "workflows": judge_model_change(candidate, stamp=before) if moves else [],
    }


def _moves(before: Optional[str], after: Optional[str]) -> bool:
    """Whether an assignment write moved the investigation model to one it can judge."""
    return after is not None and after != before


_JUDGE_PENDING = "autonomy.model_judge_pending"


def _judge_after_commit(session) -> None:
    if session.in_nested_transaction() or _JUDGE_PENDING not in session.info:
        return
    before = session.info.pop(_JUDGE_PENDING)
    try:
        after = investigation_model()
        if not _moves(before, after):
            return
        judge_model_change(after, stamp=before, write=True)
    except Exception:  # noqa: BLE001 - the assignment saved; the handoff judges again
        logger.exception("model change not judged after an assignment write")


def _judge_after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_JUDGE_PENDING, None)


def judge_after_commit(session, before: Optional[str]) -> None:
    """Judge Act workflows once ``session`` commits, if the investigation model
    moved from ``before``. The registry reads through its own session, so it
    only sees the new row after the commit."""
    session.info.setdefault(_JUDGE_PENDING, before)
    if not event.contains(session, "after_commit", _judge_after_commit):
        event.listen(session, "after_commit", _judge_after_commit)
        event.listen(session, "after_rollback", _judge_after_rollback)


# Effect classes, loosest first (#3304). A name with no row is unclassified.
READ = "read"
READ_UNVERIFIED = "read_unverified"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
EFFECT_CLASSES = (READ, READ_UNVERIFIED, REVERSIBLE, IRREVERSIBLE)
# What chat may reach, and what a direct-action call runs without an approval for.
_READS = frozenset({READ, READ_UNVERIFIED})
APPROVAL_TOOL = "create_approval_action"
TOOL_CLASSIFIED = "autonomy.tool_classified"
_SYSTEM = "system"

_own: Optional[frozenset[str]] = None


def own_tool_names() -> frozenset[str]:
    """Names Vigil answers itself: its built-ins and its own MCP server's.

    Neither is an outside tool, so neither has a class. The set is fixed at
    import, so it is remembered once both halves are read.
    """
    global _own
    if _own is not None:
        return _own
    from core.agents.tool_registry import FACTS_TOOL
    from core.llm.tool_schemas import ALL_TOOLS

    # The run-start fact read is in no catalogue but is Vigil's own: unclassified,
    # a run no person started (an alert's investigation, a scheduled hunt) was refused it.
    names = {t["name"] for t in ALL_TOOLS if t.get("name")} | {FACTS_TOOL}
    try:
        from core.integrations.mcp import in_process

        names |= {t["name"] for t in in_process.list_tools()}
    except Exception:  # noqa: BLE001 - not remembered; the next call retries
        logger.exception("Vigil's own MCP tool names were not read")
        return frozenset(names)
    _own = frozenset(names)
    return _own


def stored_classes(session, names: Iterable[str]) -> dict[str, str]:
    """The rows for ``names``. A name with no row is absent."""
    rows = session.execute(
        select(ToolEffectClass.tool_name, ToolEffectClass.effect_class).where(
            ToolEffectClass.tool_name.in_(list(names))
        )
    )
    return {name: effect_class for name, effect_class in rows}


def _read_classes(names: list[str]) -> dict[str, str]:
    with unit_of_work() as session:
        return stored_classes(session, names)


def tool_classes(names: Iterable[str]) -> dict[str, Optional[str]]:
    """Each outside name's class, or None when it is unclassified. One query.

    Names Vigil answers itself are left out. A store that cannot be read
    leaves every name unclassified, which asks first.
    """
    own = own_tool_names()
    outside = sorted({n for n in names if n and n not in own})
    if not outside:
        return {}
    try:
        stored = _read_classes(outside)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "tool classes not read; %d treated as unclassified: %s", len(outside), exc
        )
        stored = {}
    return {name: stored.get(name) for name in outside}


def is_read(effect_class: Optional[str]) -> bool:
    return effect_class in _READS


def gated_tools(names: Iterable[str]) -> frozenset[str]:
    """Outside tools that wait for an approval: reversible, irreversible, unclassified."""
    return frozenset(
        name for name, effect in tool_classes(names).items() if not is_read(effect)
    )


def asks_first(names: Iterable[str]) -> frozenset[str]:
    """The approval tool, and every gated outside tool, among ``names``."""
    names = list(names)
    return gated_tools(names) | ({APPROVAL_TOOL} & set(names))


STOPPED = "Stopped"


# Own tools that change what Vigil detects: held to the ceiling as reversible
# (#3949). Fixed here, not a stored class an operator could loosen to read.
def _own_reversible(tool: str, args: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why the call is refused unless the run may act, or None for an ordinary write."""
    held = "and the autonomy ceiling does not let a run no person started make it"
    if tool == "detection_rule_exception":
        return f"{tool} changes what a rule detects, {held}"
    exceptions = (args or {}).get("exceptions")
    if tool == "save_detection_rule" and isinstance(exceptions, Mapping):
        if any(exceptions.values()):
            return (
                f"{tool} with exceptions changes what a rule detects, {held}; "
                "save it without exceptions"
            )
    return None


def _exception_values(tool: str, args: Optional[Mapping[str, Any]]) -> list:
    """Each ``match`` value of the exceptions a call adds; removals are not read."""
    args = args or {}
    if tool == "detection_rule_exception":
        adds = [args] if args.get("action") == "add" else []
    else:
        per_copy = args.get("exceptions")
        per_copy = per_copy.values() if isinstance(per_copy, Mapping) else []
        adds = [e for items in per_copy if isinstance(items, list) for e in items]
    return [
        value
        for add in adds
        if isinstance(add, Mapping) and isinstance(add.get("match"), Mapping)
        for value in add["match"].values()
        if isinstance(value, str)
    ]


def _string_values(value: Any) -> Iterable[str]:
    """Every string value in nested dicts and lists; keys are not read."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)


def unattended_refusal(
    tool: str,
    *,
    writes: bool,
    run_id: Optional[str] = None,
    args: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Why a run no person started may not make this call, or None (#3306).

    Reads run, stopped or not. ``writes`` says whether one of Vigil's own tools
    changes a record; those run unless stopped, except a rule exception, which
    needs the effective act a reversible call does (#3949). An outside call runs
    only under the effective act its class needs, for the run's own workflow
    (#3624). A ceiling that cannot be read grants nothing. Where the act would
    pass, an exception add or outside call naming a declared crown jewel is
    refused, and a dismissal of a crown jewel's finding always is (#5227); so is
    a containment act a person undid or rejected on that target (#5273).
    """
    classes = tool_classes([tool])
    own = tool not in classes
    if (own and not writes) or (not own and is_read(classes[tool])):
        return None
    held = try_read_stored_ceiling()
    if held is None:
        return "the autonomy ceiling could not be read"
    stored, _found = held
    if stored.stopped:
        return STOPPED
    if own:
        # No ceiling gates a dismissal; a crown jewel's still waits for a person.
        if tool == "dismiss_finding" and (
            jewel := crown_jewel_held(
                lambda: finding_keys(finding_ids=[(args or {}).get("finding_id")]),
                stored,
            )
        ):
            return jewel
        refusal = _own_reversible(tool, args)
        if refusal is None:
            return None
        workflow, read = _workflow_held(run_id)
        if read and effective_act(stored, REVERSIBLE, workflow):
            return crown_jewel_held(lambda: _exception_values(tool, args), stored)
        return refusal if read else "the run's workflow could not be read"
    effect = classes[tool]
    if effect in (REVERSIBLE, IRREVERSIBLE):
        workflow, read = _workflow_held(run_id)
        if not read:
            return "the run's workflow could not be read"
        if effective_act(stored, effect, workflow):
            # Exact selectors only: a name fitted to a pattern can't shield a host.
            if jewel := crown_jewel_held(
                lambda: _string_values(args), stored, exact=True
            ):
                return jewel
            # A person's undo or rejection of this act on this target holds it (#5273).
            from core.response.reversals import reversal_held

            return reversal_held(tool, _string_values(args))
    return (
        f"{tool} is {effect or 'unclassified'} and the autonomy ceiling "
        "does not let a run no person started take it"
    )


def loosens(before: Optional[str], after: str) -> bool:
    """Classifying an unclassified name counts as loosening."""
    return before is None or EFFECT_CLASSES.index(after) < EFFECT_CLASSES.index(before)


def store_tool_class(
    session, name: str, before: Optional[str], after: str, actor: str
) -> None:
    """Write one class and its trail entry in the caller's transaction."""
    from core.auth import trail_events

    row = session.get(ToolEffectClass, name)
    if row is None:
        session.add(
            ToolEffectClass(tool_name=name, effect_class=after, classified_by=actor)
        )
    else:
        row.effect_class = after
        row.classified_by = actor
        row.classified_at = utcnow()
    session.flush()
    trail_events.record(
        session,
        TOOL_CLASSIFIED,
        actor=actor,
        tool=name,
        class_before=before or "unclassified",
        class_after=after,
    )


def tighten_from_hints(server: str, tools: Iterable[dict]) -> bool:
    """``destructiveHint: true`` raises an existing row to irreversible.

    It never inserts a row and never lowers one; ``readOnlyHint`` is not read.
    Best effort: False when the store could not be written, so the caller can
    try again; a server's connect does not fail on it.
    """
    hinted = sorted(
        f"{server}_{tool['name']}"
        for tool in tools
        if tool.get("name")
        and isinstance(tool.get("annotations"), dict)
        and tool["annotations"].get("destructiveHint") is True
    )
    if not hinted:
        return True
    try:
        with unit_of_work() as session:
            rows = (
                session.execute(
                    select(ToolEffectClass)
                    .where(
                        ToolEffectClass.tool_name.in_(hinted),
                        ToolEffectClass.effect_class != IRREVERSIBLE,
                    )
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for row in rows:
                store_tool_class(
                    session, row.tool_name, row.effect_class, IRREVERSIBLE, _SYSTEM
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("destructiveHint from %s not applied: %s", server, exc)
        return False
