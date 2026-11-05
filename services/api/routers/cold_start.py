"""Day-one cold start: compute a baseline from history the customer already has.

A run is a dry run. Its alerts and tickets stay in the cold start tables, and going
live shows its baseline to people beside each triage verdict. The model never sees it,
and nothing here closes an alert.
"""

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from core.auth.current_user import get_current_user
from core.auth.permissions import deny_unless, permission_gate
from core.coldstart import collect, runner, store, tickets
from core.config import get_settings, load_integrations_config
from core.deps import provide_process_health
from core.federation import registry
from core.llm.target import resolve_dispatch
from core.platform.process_health import ProcessHealth
from core.routing import Auth, RouterMeta
from core.storage.models import User
from core.storage.models.cold_start import InvalidTransition, RunStatus
from services.api.routers.ingestion import EXTENSION_FORMATS, _spool_upload

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/cold-start",
    tags=["cold-start"],
    auth=Auth.REQUIRED,
)

_FINDINGS_WRITE = [permission_gate("findings.write")]

DEFAULT_HISTORY_DAYS = 60
DEFAULT_REVIEW_DAYS = 1
DEFAULT_JUDGE_LIMIT = 100


class CreateRun(BaseModel):
    source: str
    history_days: int = Field(default=DEFAULT_HISTORY_DAYS, ge=1, le=365)
    review_days: Optional[int] = Field(default=None, ge=1, le=90)
    judge_limit: int = Field(default=DEFAULT_JUDGE_LIMIT, ge=0, le=5000)


class ReviewRequest(BaseModel):
    verdict: Literal["agree", "not_benign"]


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except store.RunNotFound:
        raise HTTPException(status_code=404, detail="No such cold start run.") from None
    except store.GroupNotFound:
        raise HTTPException(
            status_code=404, detail="No such alert kind in this run's review window."
        ) from None
    except (
        store.ActiveRunExists,
        store.RunBusy,
        runner.NothingToBuild,
        InvalidTransition,
    ) as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except runner.SourceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


REVIEW_REBUILD_WAIT_SECONDS = 4
SOURCE_LABELS = {
    "crowdstrike": "CrowdStrike Falcon",
    "microsoft_defender": "Microsoft Defender for Endpoint",
    "elastic": "Elastic, or a Wazuh indexer",
    "opensearch": "OpenSearch, or a Wazuh indexer",
    "aws_security_hub": "AWS Security Hub",
    "azure_sentinel": "Microsoft Sentinel",
    "splunk": "Splunk",
}


def _source_label(source_id: str) -> str:
    return SOURCE_LABELS.get(source_id, source_id.replace("_", " ").title())


def _sources() -> List[Dict[str, Any]]:
    return [
        *(
            {
                "id": adapter.name,
                "label": _source_label(adapter.name),
                "kind": "federation",
                "configured": adapter.is_configured(),
            }
            for adapter in registry.list_adapters()
        ),
        {
            "id": collect.UPLOAD_SOURCE,
            "label": "Upload",
            "kind": "upload",
            "configured": True,
        },
    ]


def _triage_model() -> Optional[Dict[str, str]]:
    target = resolve_dispatch("triage")
    if target is None:
        return None
    provider, model = target
    return {"provider": provider.provider_type, "model": model}


def _loglm_status() -> Dict[str, bool]:
    """What the Settings LogLM card shows: a connector URL and a last test that passed."""
    loaded = load_integrations_config()
    configured = bool((loaded["integrations"].get("loglm") or {}).get("connectorUrl"))
    last_test = (loaded.get("last_test") or {}).get("loglm") or {}
    return {
        "configured": configured,
        "connected": configured and last_test.get("success") is True,
    }


@router.get("/sources")
async def list_sources(
    processes: ProcessHealth = Depends(provide_process_health),
) -> Dict[str, Any]:
    """``daemon`` is here because the daemon is the only reader of a live baseline, and
    GET /api/processes needs platform.read, which going live does not."""
    return {
        "sources": await run_in_threadpool(_sources),
        "model": await run_in_threadpool(_triage_model),
        "loglm": await run_in_threadpool(_loglm_status),
        "daemon": {
            "state": (await processes.read())["daemon"],
            "auto_triage": get_settings().daemon_auto_triage,
        },
    }


def _check_source(source: str) -> None:
    if source == collect.UPLOAD_SOURCE:
        return
    adapter = registry.get_adapter(source)
    if adapter is None:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}.")
    if not adapter.is_configured():
        raise HTTPException(status_code=400, detail=f"{source} is not configured.")


@router.post("/runs", status_code=202, dependencies=_FINDINGS_WRITE)
async def create_run(
    payload: CreateRun, current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    await run_in_threadpool(_check_source, payload.source)
    with _http_errors():
        run_id = await run_in_threadpool(
            store.create_run,
            source=payload.source,
            history_days=payload.history_days,
            review_days=payload.review_days or DEFAULT_REVIEW_DAYS,
            judge_limit=payload.judge_limit,
            created_by=str(current_user.user_id),
        )
        if payload.source != collect.UPLOAD_SOURCE:
            await runner.start(run_id)
        return await run_in_threadpool(store.run_summary, run_id)


@router.get("/runs/latest")
async def latest_run() -> Dict[str, Any]:
    summary = await run_in_threadpool(store.latest_summary)
    if summary is None:
        raise HTTPException(status_code=404, detail="No cold start run yet.")
    return summary


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    with _http_errors():
        return await run_in_threadpool(store.run_summary, run_id)


def _file_key(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_alert_file(run_id: str, path: Path, fmt: str) -> Dict[str, Any]:
    parsed = collect.parse_alert_file(path, fmt)
    store.insert_uploaded_alerts(run_id, parsed.rows)
    store.record_drops(run_id, "alerts", _file_key(path), parsed.dropped)
    return {"kept": len(parsed.rows), "dropped": dict(parsed.dropped)}


def _store_ticket_file(run_id: str, path: Path, fmt: str) -> Dict[str, Any]:
    parsed = tickets.parse_ticket_file(path, fmt)
    store.insert_tickets(run_id, parsed.kept)
    store.record_drops(run_id, "tickets", _file_key(path), parsed.dropped)
    return {"kept": len(parsed.kept), "dropped": dict(parsed.dropped)}


async def _ingest_upload(
    run_id: str,
    file: UploadFile,
    allowed: frozenset,
    store_file: Callable[[str, Path, str], Dict[str, Any]],
) -> Dict[str, Any]:
    fmt = EXTENSION_FORMATS.get(Path(file.filename or "").suffix.lower())
    if fmt not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Use one of these file types: {', '.join(sorted(allowed))}.",
        )
    path = await _spool_upload(file, f".{fmt}")
    try:
        return await run_in_threadpool(store_file, run_id, path, fmt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    finally:
        path.unlink(missing_ok=True)


@router.post("/runs/{run_id}/alerts", dependencies=_FINDINGS_WRITE)
async def upload_alerts(run_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    with _http_errors():
        params = await run_in_threadpool(store.run_params, run_id)
    if params.source != collect.UPLOAD_SOURCE:
        raise HTTPException(
            status_code=409, detail="Only an upload run takes alert files."
        )
    if params.status not in (RunStatus.COLLECTING, RunStatus.READY):
        raise HTTPException(
            status_code=409,
            detail=f"A {params.status.value} run does not take alert files.",
        )
    return await _ingest_upload(run_id, file, collect.FORMATS, _store_alert_file)


@router.post("/runs/{run_id}/tickets", dependencies=_FINDINGS_WRITE)
async def upload_tickets(run_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    with _http_errors():
        params = await run_in_threadpool(store.run_params, run_id)
    if params.status is RunStatus.FAILED:
        raise HTTPException(status_code=409, detail="This run failed.")
    return await _ingest_upload(run_id, file, tickets.FORMATS, _store_ticket_file)


@router.post("/runs/{run_id}/rebuild", dependencies=_FINDINGS_WRITE)
async def rebuild(run_id: str) -> Dict[str, Any]:
    """An upload run still collecting has its files now, so this starts it. After that,
    it recomputes in place and keeps reviews and LLM results."""
    with _http_errors():
        params = await run_in_threadpool(store.run_params, run_id)
        if (
            params.status is RunStatus.COLLECTING
            and params.source == collect.UPLOAD_SOURCE
        ):
            await runner.start(run_id)
        elif params.status in (RunStatus.READY, RunStatus.LIVE):
            await run_in_threadpool(runner.rebuild_while_idle, run_id)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"A {params.status.value} run cannot be rebuilt.",
            )
        return await run_in_threadpool(store.run_summary, run_id)


@router.post("/runs/{run_id}/judge", dependencies=_FINDINGS_WRITE)
async def judge(run_id: str) -> Dict[str, Any]:
    """Finish the run from wherever it stopped: a process that died left it collecting
    or judging with nobody working on it, and a run at ready judges what is new."""
    with _http_errors():
        params = await run_in_threadpool(store.run_params, run_id)
        if params.status in (RunStatus.LIVE, RunStatus.FAILED):
            raise HTTPException(
                status_code=409, detail=f"A {params.status.value} run cannot be judged."
            )
        await runner.start(run_id)
        return await run_in_threadpool(store.run_summary, run_id)


@router.get("/runs/{run_id}/groups")
async def list_groups(run_id: str) -> Dict[str, Any]:
    with _http_errors():
        return {"groups": await run_in_threadpool(store.groups, run_id)}


@router.post("/runs/{run_id}/groups/{key}/review", dependencies=_FINDINGS_WRITE)
async def review_group(
    run_id: str,
    key: str,
    payload: ReviewRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    with _http_errors():
        await run_in_threadpool(
            store.review_group, run_id, key, payload.verdict, str(current_user.user_id)
        )
        try:
            # A build that read the reviews just before this one was saved holds the lease
            # for a moment, so wait it out instead of leaving the verdict unapplied. A long
            # judging run outlasts the wait, and rebuilds from the stored verdict at its end.
            await run_in_threadpool(
                runner.rebuild_while_idle, run_id, REVIEW_REBUILD_WAIT_SECONDS
            )
        except store.RunBusy:
            pass
        return {"groups": await run_in_threadpool(store.groups, run_id)}


@router.post("/runs/{run_id}/go-live", dependencies=_FINDINGS_WRITE)
async def go_live(
    run_id: str, current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    with _http_errors():
        params = await run_in_threadpool(store.run_params, run_id)
        if params.source != collect.UPLOAD_SOURCE:
            # Going live turns a source on, which is an integration write.
            await run_in_threadpool(
                deny_unless, str(current_user.user_id), "integrations.write"
            )
        await run_in_threadpool(runner.go_live, run_id, str(current_user.user_id))
        return await run_in_threadpool(store.run_summary, run_id)
