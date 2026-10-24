"""HMAC-SHA256 signed JSON webhook receiving, shared by the Darktrace and Cloudy routers."""

import hmac
import json
import logging
from hashlib import sha256
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, status

from core.webhook_rejections import (
    BAD_SIGNATURE,
    MISSING_SIGNATURE,
    NO_SECRET,
    SECRET_LOOKUP_FAILED,
    record_rejection,
)

logger = logging.getLogger(__name__)


def reject(
    request: Request,
    endpoint: str,
    reason: str,
    status_code: int,
    detail: str,
    exc: Optional[BaseException] = None,
) -> HTTPException:
    record_rejection(
        endpoint, reason, request.client.host if request.client else None, exc=exc
    )
    return HTTPException(status_code=status_code, detail=detail)


async def read_signed_json(
    request: Request,
    signature: Optional[str],
    *,
    endpoint: str,
    vendor: str,
    header: str,
    secret_name: str,
    get_secret: Callable[[], Optional[str]],
    get_max_body_bytes: Callable[[], int],
) -> Dict[str, Any]:
    try:
        secret = get_secret()
    except Exception as exc:  # noqa: BLE001
        raise reject(
            request,
            endpoint,
            SECRET_LOOKUP_FAILED,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{vendor} webhook secret lookup failed",
            exc,
        )
    if not secret:
        raise reject(
            request,
            endpoint,
            NO_SECRET,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{vendor} webhook receiver not configured ({secret_name} missing)",
        )
    raw = await request.body()
    max_body_bytes = get_max_body_bytes()
    if len(raw) > max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Body exceeds {max_body_bytes} bytes",
        )
    expected = hmac.new(secret.encode("utf-8"), raw, sha256).hexdigest().encode()
    # Bytes, because compare_digest raises TypeError on a non-ASCII str, and
    # Starlette decodes header bytes as latin-1.
    if not signature or not hmac.compare_digest(
        expected, signature.split("=", 1)[-1].strip().encode("latin-1")
    ):
        raise reject(
            request,
            endpoint,
            BAD_SIGNATURE if signature else MISSING_SIGNATURE,
            status.HTTP_401_UNAUTHORIZED,
            f"Invalid or missing {header}",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid JSON body",
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook payload must be a JSON object",
        )
    return payload


def ingest(service: Any, finding: Optional[Dict], label: str) -> Dict[str, Any]:
    if finding is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unable to transform {label} payload",
        )
    if not service.ingestion_service.ingest_finding(finding):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Finding was not persisted",
        )
    logger.info(
        "%s ingested: finding_id=%s data_source=%s",
        label,
        finding.get("finding_id"),
        finding.get("data_source"),
    )
    return {"accepted": True, "finding_id": finding["finding_id"]}
