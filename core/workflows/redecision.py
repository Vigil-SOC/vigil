"""One pre-install closure, re-decided by the live investigation agent (#5092).

The run is an ordinary ``investigate`` workflow. ``trigger_context`` carries
the mark (``re_decision``), the import, the ticket, the source and the close,
and no ``case_id``. The batch calls ``start_redecision`` with the run id it
already committed, and reads ``redecision_result`` off the terminal. Nothing
here writes an investigation, a Case, a Finding or an approval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select

from core.agents.attribution import (
    TrailWriteError,
    note_run_start,
    note_terminal,
    plugin_actor,
)
from core.agents.operator_manifest import added_request
from core.agents.queue import build_start_job, enqueue_run, new_run_id
from core.auth import tool_principal
from core.auth.principals import SYSTEM_PRINCIPAL_ID
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion
from core.memory.imported_history import _keys, _severity
from core.memory.recall_contract import bounded_prose
from core.reporting.analytics_service import _NOT_REAL, _REAL
from core.storage.models.history import HistoryAlert, HistoryTicket
from core.storage.unit_of_work import unit_of_work
from core.workflows.redecision_hold import MARK, WORKFLOW_ID, as_utc, remember_close
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import (
    INVESTIGATE_RUN_KIND,
    WORKFLOW_SCHEME,
    WorkflowsService,
)

logger = logging.getLogger(__name__)

NO_ALERT_ROW = "could not re-check: no alert row"
COULD_NOT = "could not re-check"

_CONNECTED = {
    "microsoft_defender": "defender",
    "azure_sentinel": "sentinel",
}


def redecision_result(
    outcome: str,
    *,
    disposition: Optional[str] = None,
    reason: str = "",
    proposed_actions: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """The batch's reading of an investigate terminal.

    ``malicious`` and ``suspicious`` are real, ``benign`` and ``false_positive``
    are not real, and ``inconclusive`` and ``needs_review`` are undetermined.
    Anything that did not complete is ``could not re-decide``, with its reason.
    ``proposed_actions`` stay on the result.
    """
    actions = list(proposed_actions or [])
    if outcome != "completed":
        why = (reason or "").strip()
        text = f"could not re-decide: {why}" if why else "could not re-decide"
        return {"result": text, "proposed_actions": actions}
    label = (disposition or "").strip().lower()
    if label in _REAL:
        result = "real"
    elif label in _NOT_REAL:
        result = "not real"
    else:
        result = "undetermined"
    return {"result": result, "proposed_actions": actions}


def _content(
    *,
    time: Any,
    title: Any,
    rule: Any,
    severity: Any,
    entity_keys: List[str],
    description: Any,
) -> Dict[str, Any]:
    when = time.isoformat() if isinstance(time, datetime) else (time or "")
    rule_id = None if rule in (None, "") else str(rule)[:500]
    return {
        "time": "" if when is None else str(when),
        "title": bounded_prose(title or ""),
        "rule": rule_id or "",
        "severity": _severity(severity),
        "entity_keys": list(entity_keys),
        "description": bounded_prose(description or ""),
    }


def _keys_of(entity_context: Mapping[str, Any]) -> List[str]:
    kept = {
        name: entity_context.get(name) or []
        for name in ("src_ips", "hostnames", "usernames", "domains", "file_hashes")
    }
    return _keys({"entity_context": kept})


def _prompt(alerts: Sequence[Mapping[str, Any]]) -> str:
    """The run's opening text: the allow-listed fields, and nothing else."""
    blocks = []
    for alert in alerts:
        keys = alert.get("entity_keys") or []
        shown = ", ".join(str(key) for key in keys)
        blocks.append(
            "\n".join(
                (
                    f"time: {alert.get('time') or ''}",
                    f"title: {alert.get('title') or ''}",
                    f"rule: {alert.get('rule') or ''}",
                    f"severity: {alert.get('severity') or ''}",
                    f"entity keys: {shown}",
                    f"description: {alert.get('description') or ''}",
                )
            )
        )
    return "\n\n".join(blocks)


def _file_alerts(ticket: HistoryTicket) -> List[Dict[str, Any]]:
    refs = [ref for ref in (ticket.alert_ids or []) if ref]
    if not refs:
        return []
    with unit_of_work() as session:
        rows = session.scalars(
            select(HistoryAlert)
            .where(
                HistoryAlert.import_id == ticket.import_id,
                HistoryAlert.alert_id.in_(refs),
            )
            .order_by(HistoryAlert.alert_id)
        ).all()
    return [
        _content(
            time=row.alert_time,
            title=row.title,
            rule=row.rule_id,
            severity=row.severity,
            entity_keys=list(row.entity_keys or []),
            description=row.description,
        )
        for row in rows
    ]


def _defender_alerts(alert: Mapping[str, Any]) -> List[Dict[str, Any]]:
    finding = MicrosoftDefenderIngestion().transform_alert_to_finding(dict(alert)) or {}
    context = finding.get("entity_context") or {}
    return [
        _content(
            time=alert.get("alertCreationTime"),
            title=alert.get("title"),
            rule=alert.get("detectorId"),
            severity=alert.get("severity"),
            entity_keys=_keys_of(context if isinstance(context, Mapping) else {}),
            description=alert.get("description"),
        )
    ]


def _sentinel_alerts(incident: Mapping[str, Any]) -> List[Dict[str, Any]]:
    context = incident.get("entity_context") or {}
    rows = [
        _content(
            time=incident.get("time"),
            title=incident.get("title"),
            rule=incident.get("rule"),
            severity=incident.get("severity"),
            entity_keys=_keys_of(context if isinstance(context, Mapping) else {}),
            description=incident.get("description"),
        )
    ]
    for alert in incident.get("alerts") or []:
        if not isinstance(alert, Mapping):
            continue
        rows.append(
            _content(
                time=alert.get("time"),
                title=alert.get("title"),
                rule=None,
                severity=alert.get("severity"),
                entity_keys=[],
                description=alert.get("description"),
            )
        )
    return rows


def _ticket(import_id: str, ticket_id: str) -> Optional[HistoryTicket]:
    with unit_of_work() as session:
        ticket = session.scalars(
            select(HistoryTicket).where(
                HistoryTicket.import_id == import_id,
                HistoryTicket.ticket_id == ticket_id,
            )
        ).first()
        if ticket is not None:
            session.expunge(ticket)
        return ticket


def _vendor(import_id: str, source: str) -> Optional[str]:
    if not str(import_id).endswith(":connected"):
        return None
    return _CONNECTED.get(source)


async def _vendor_alerts(kind: str, ticket_id: str) -> Optional[List[Dict[str, Any]]]:
    """Content rows, or None when the vendor no longer holds the alert."""
    if kind == "defender":
        try:
            alert = await MicrosoftDefenderIngestion().fetch_alert(ticket_id)
        except Exception:  # noqa: BLE001 — a failed fetch was not re-checked
            logger.warning("Defender alert %s could not be re-read", ticket_id)
            return None
        if not alert:
            return None
        return _defender_alerts(alert)

    try:
        incident = await asyncio.to_thread(
            AzureSentinelIngestion().fetch_incident, ticket_id
        )
    except Exception:  # noqa: BLE001 — a failed fetch was not re-checked
        logger.warning("Sentinel incident %s could not be re-read", ticket_id)
        return None
    if not incident:
        return None
    return _sentinel_alerts(incident)


def _refused(error: str) -> Dict[str, Any]:
    return {"success": False, "error": error}


async def start_redecision(
    import_id: str,
    ticket_id: str,
    *,
    triggered_by: str = "re-decision",
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Enqueue one re-decision, or return ``could not re-check`` and enqueue none.

    A file import reads ``history_alerts`` for the ticket's ``alert_ids``. No
    refs, or no matching row, is ``could not re-check: no alert row``. Defender
    is fetched by the ticket id as the alert id, Sentinel by it as the incident
    name. A gone response is ``could not re-check``.
    """
    ticket = _ticket(import_id, ticket_id)
    if ticket is None:
        return _refused(NO_ALERT_ROW)
    if ticket.closed_at is None:
        return _refused(COULD_NOT)

    source = ticket.source_system or ""
    vendor = _vendor(import_id, source)
    alerts: List[Dict[str, Any]]
    if vendor is None:
        alerts = _file_alerts(ticket)
        if not alerts:
            return _refused(NO_ALERT_ROW)
    else:
        fetched = await _vendor_alerts(vendor, ticket_id)
        if not fetched:
            return _refused(COULD_NOT)
        alerts = fetched

    close = as_utc(ticket.closed_at)
    prompt = _prompt(alerts)
    keys: List[str] = []
    for alert in alerts:
        for key in alert.get("entity_keys") or []:
            if key not in keys:
                keys.append(key)

    workflow = WorkflowsService().get_workflow(WORKFLOW_ID)
    if workflow is None:
        return _refused(COULD_NOT)

    # The batch commits this id before the call, so the ticket and the ledger
    # name one run. A caller with no id still gets one minted here.
    run_id = run_id or new_run_id()
    context = {
        MARK: True,
        "import_id": import_id,
        "ticket_id": ticket_id,
        "source": source,
        "closed_at": close.isoformat(),
        "run_kind": INVESTIGATE_RUN_KIND,
    }
    persisted = WorkflowRunService().begin_run(
        workflow_id=WORKFLOW_ID,
        workflow_name=workflow.name,
        workflow_source=workflow.source,
        workflow_version=workflow.version,
        trigger_context=context,
        triggered_by=triggered_by,
        triggered_by_principal_id=SYSTEM_PRINCIPAL_ID,
        run_id=run_id,
    )
    if not persisted:
        return _refused(COULD_NOT)

    actor = plugin_actor(INVESTIGATE_RUN_KIND)
    spec: Dict[str, Any] = {
        "arch": "",
        "playbook": f"{WORKFLOW_SCHEME}{WORKFLOW_ID}",
        "config": "",
        "prompt": prompt,
    }
    if keys:
        spec["recall_keys"] = keys
    try:
        note_run_start(
            run_id=run_id,
            actor=actor,
            origin_kind="schedule",
            origin="schedule",
            spec=spec,
        )
    except TrailWriteError as exc:
        logger.error("run %s was not recorded on the trail: %s", run_id, exc)
        WorkflowRunService().finalize_run(
            run_id, status="failed", error="the action could not be recorded"
        )
        return _refused("the action could not be recorded")

    try:
        operator = added_request(INVESTIGATE_RUN_KIND)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — the skills read is the run's, and it failed
        logger.error("run %s could not read added skills: %s", run_id, exc)
        operator = {}

    principal = tool_principal.mint(
        agent_id=actor,
        run_id=run_id,
        origin="schedule",
        ttl=tool_principal.RUN_TTL,
    )
    job = build_start_job(
        run_id=run_id,
        run_kind=INVESTIGATE_RUN_KIND,
        request={**spec, **operator},
        enqueued_by=triggered_by,
        principal=principal,
        actor=actor,
        origin="schedule",
    )
    try:
        job_id = await enqueue_run(job)
    except Exception as exc:  # noqa: BLE001 — the queue is the only remaining failure
        logger.error("failed to enqueue re-decision %s: %s", run_id, exc)
        WorkflowRunService().finalize_run(
            run_id, status="failed", error="run queue unavailable"
        )
        note_terminal(run_id, "failed", "")
        return _refused("run queue unavailable")

    remember_close(run_id, close)
    return {"success": True, "run_id": run_id, "job_id": job_id}
