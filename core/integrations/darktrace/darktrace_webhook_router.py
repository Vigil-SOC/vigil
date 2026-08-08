"""
Darktrace inbound webhook receiver.

Accepts pushes from Darktrace (SaaS tenants and on-prem master appliances)
for three alert streams — Model Breach, AI Analyst, and System Status —
verifies an HMAC-SHA256 signature against a shared secret, transforms each
payload into a Vigil finding via ``DarktraceIngestionService``, and ingests
it through the shared ``IngestionService``.

Endpoints:
    POST /api/webhooks/darktrace/model-breach
    POST /api/webhooks/darktrace/ai-analyst
    POST /api/webhooks/darktrace/system-status
    GET  /api/webhooks/darktrace/health

Signature header: ``X-Darktrace-Signature`` (hex HMAC-SHA256 of raw body).
"""

import asyncio
import hmac
import logging
from hashlib import sha256
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from core.integrations.darktrace.ingestion import DarktraceIngestionService
from core.routing import Auth, RouterMeta
from core.config import get_settings
from core.secrets import get_secret

logger = logging.getLogger(__name__)

router = APIRouter()


def darktrace_enabled() -> bool:
    """Master flag for the Darktrace webhook receiver.

    Off unless explicitly enabled. Lives here rather than inline in
    ``services/api/main.py`` so the gate travels with the receiver it guards and
    can be unit-tested directly (issue #478). Reads the typed ``Settings``
    field rather than the raw env var so there is a single source of truth
    for the flag, matching ``cloudflare_webhooks.cloudy_ingestion_enabled``.
    """
    return get_settings().darktrace_enabled


ROUTER_META = RouterMeta(
    prefix="/api/webhooks/darktrace",
    tags=["darktrace"],
    auth=Auth.PUBLIC_WEBHOOK,
    reason=(
        "Inbound receiver for Darktrace pushes — the caller is a machine, so "
        "there is no session to authenticate. Each endpoint verifies an "
        "HMAC-SHA256 X-Darktrace-Signature and fails closed when no shared "
        "secret is configured."
    ),
    # env.example and docs/integrations/DARKTRACE.md document DARKTRACE_ENABLED
    # as the on/off toggle; leaving it unset must leave the receiver off.
    enabled=darktrace_enabled,
)


def _get_settings() -> Dict[str, Any]:
    """Read darktrace.settings from system_config (DB). Falls back to env vars."""
    try:
        from core.storage.config_service import get_config_service
        value = get_config_service().get_system_config("darktrace.settings") or {}
        if value:
            return value
    except Exception as exc:  # noqa: BLE001
        logger.debug("darktrace.settings read failed, using env: %s", exc)
    return {}


def _get_max_body_bytes() -> int:
    settings = _get_settings()
    try:
        kb = int(settings.get("max_body_kb") or get_settings().darktrace_max_body_kb)
    except (TypeError, ValueError):
        kb = 1024
    return max(1, kb) * 1024


# Read at request time, not import time, so a secret saved in the UI takes
# effect without a restart.
def _get_secret() -> Optional[str]:
    try:
        return get_secret("DARKTRACE_WEBHOOK_SECRET") or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("DARKTRACE_WEBHOOK_SECRET lookup failed: %s", exc)
        return None


def _get_console_url() -> str:
    url = _get_settings().get("url")
    if url:
        return str(url)
    return get_settings().darktrace_url


def _verify_signature(raw_body: bytes, provided: Optional[str]) -> bool:
    secret = _get_secret()
    if not secret:
        # Fail closed: without a configured secret we cannot authenticate.
        return False
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    # Strip common prefix if Darktrace wraps signature (e.g. "sha256=...").
    clean = provided.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, clean)


async def _read_and_verify(request: Request, signature: Optional[str]) -> bytes:
    if not _get_secret():
        logger.error("DARKTRACE_WEBHOOK_SECRET not configured; rejecting webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Darktrace webhook receiver not configured",
        )
    raw = await request.body()
    if len(raw) > _get_max_body_bytes():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Body exceeds {_get_max_body_bytes()} bytes",
        )
    if not _verify_signature(raw, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Darktrace-Signature",
        )
    return raw


def _parse_json(raw: bytes) -> Dict:
    import json

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid JSON body: {e}",
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook payload must be a JSON object",
        )
    return payload


def _ingest(
    payload: Dict,
    transform: Callable[[DarktraceIngestionService, Dict], Optional[Dict]],
    alert_type: str,
) -> Dict:
    service = DarktraceIngestionService(console_url=_get_console_url())
    finding = transform(service, payload)
    if finding is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unable to transform Darktrace {alert_type} payload",
        )
    try:
        ok = service.ingestion_service.ingest_finding(finding)
    except Exception as e:
        logger.exception("Darktrace %s ingestion failed", alert_type)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion error: {e}",
        )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Finding was not persisted",
        )
    logger.info(
        "Darktrace %s ingested: finding_id=%s",
        alert_type,
        finding.get("finding_id"),
    )
    return {"accepted": True, "finding_id": finding["finding_id"]}


@router.get("/health")
async def health() -> Dict:
    """Liveness probe for Darktrace's webhook test feature."""
    return {
        "status": "ok",
        "receiver": "darktrace",
        "secret_configured": _get_secret() is not None,
    }


@router.post("/model-breach", status_code=status.HTTP_202_ACCEPTED)
async def model_breach(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    raw = await _read_and_verify(request, x_darktrace_signature)
    payload = _parse_json(raw)
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_model_breach(p),
        "model-breach",
    )


@router.post("/ai-analyst", status_code=status.HTTP_202_ACCEPTED)
async def ai_analyst(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    raw = await _read_and_verify(request, x_darktrace_signature)
    payload = _parse_json(raw)
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_ai_analyst(p),
        "ai-analyst",
    )


@router.post("/system-status", status_code=status.HTTP_202_ACCEPTED)
async def system_status(
    request: Request,
    x_darktrace_signature: Optional[str] = Header(default=None),
) -> Dict:
    raw = await _read_and_verify(request, x_darktrace_signature)
    payload = _parse_json(raw)
    return await asyncio.to_thread(
        _ingest,
        payload,
        lambda svc, p: svc.transform_system_status(p),
        "system-status",
    )
