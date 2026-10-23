"""VStrike (CloudCurrent) integration API.

Endpoints:
  - POST /findings           Receive VStrike-enriched findings (push)
  - GET  /health             Outbound reachability check
  - GET  /topology/asset/{id}  Proxy to VStrike topology lookup

Inbound push is authenticated with a Bearer API key stored via the secrets
manager under `VSTRIKE_INBOUND_API_KEY`. When `DEV_MODE=true` the auth check
is bypassed (matches the rest of the Vigil codebase).
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from core.config import get_settings
from core.integrations.vstrike.client import (
    get_vstrike_service,
)
from core.integrations.vstrike.schemas import (
    VStrikeFindingResult,
    VStrikeHealthResponse,
    VStrikePushRequest,
    VStrikePushResponse,
)
from core.routing import Auth, RouterMeta
from core.secrets import get_secret
from core.storage.database_data_service import DatabaseDataService
from services.api.errors import INTERNAL_ERROR_DETAIL
from services.api.middleware.auth import get_current_active_user

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/integrations/vstrike",
    tags=["vstrike"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "Inbound /findings uses its own bearer API key; every management, UI "
        "and proxy route hangs off the nested authenticated_router, which "
        "carries its own dependency."
    ),
)
authenticated_router = APIRouter(dependencies=[Depends(get_current_active_user)])
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


def _is_dev_mode() -> bool:
    return get_settings().dev_mode


def _expected_inbound_key() -> Optional[str]:
    try:
        return get_secret("VSTRIKE_INBOUND_API_KEY") or None
    except Exception as e:
        logger.debug("Could not read VSTRIKE_INBOUND_API_KEY from secrets: %s", e)
        return None


def verify_inbound_key(
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Bearer-token dependency for the inbound push endpoint.

    Bypassed when `DEV_MODE=true`. Returns 401 otherwise when the header is
    missing or the token does not match the configured key. Returns 503 if
    no key is configured and DEV_MODE is off (we refuse to run open).
    """
    if _is_dev_mode():
        return

    expected = _expected_inbound_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "VStrike inbound API key not configured. Set "
                "VSTRIKE_INBOUND_API_KEY or enable DEV_MODE."
            ),
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    # compare_digest rather than !=, which returns on the first differing byte.
    # Bytes, because on a str holding any non-ASCII character it raises TypeError.
    if not hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    ):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@router.post("/findings", response_model=VStrikePushResponse)
def ingest_findings(
    request: VStrikePushRequest,
    _auth: None = Depends(verify_inbound_key),
) -> VStrikePushResponse:
    """Receive a batch of VStrike-enriched findings.

    For each finding:
      - If it exists in Vigil, merge VStrike enrichment into
        `entity_context["vstrike"]` (read-modify-write to avoid clobbering
        other keys) and update MITRE fields if supplied.
      - Otherwise, create it with `data_source="vstrike"` if enough fields
        are present (timestamp + anomaly_score); fail the finding otherwise.

    When `auto_cluster_cases` is true, upserted findings are grouped into
    cases keyed by `(segment, attack_path[0] or asset_id)`.
    """
    results: list[VStrikeFindingResult] = []
    updated = 0
    created = 0
    failed = 0
    upserted_ids: list[str] = []

    for item in request.findings:
        try:
            enrichment_dict = item.vstrike_enrichment.model_dump(mode="json")
            existing = data_service.get_finding(item.finding_id)
            if existing is not None:
                existing_ctx = existing.get("entity_context") or {}
                if not isinstance(existing_ctx, dict):
                    existing_ctx = {}
                merged_ctx = dict(existing_ctx)
                if item.entity_context_extra:
                    merged_ctx.update(item.entity_context_extra)
                merged_ctx["vstrike"] = enrichment_dict

                updates: dict = {"entity_context": merged_ctx}
                if item.mitre_predictions is not None:
                    updates["mitre_predictions"] = item.mitre_predictions
                if item.predicted_techniques is not None:
                    updates["predicted_techniques"] = item.predicted_techniques
                if item.severity is not None:
                    updates["severity"] = item.severity
                if item.description is not None:
                    updates["description"] = item.description

                success = data_service.update_finding(item.finding_id, **updates)
                if success:
                    updated += 1
                    upserted_ids.append(item.finding_id)
                    results.append(
                        VStrikeFindingResult(
                            finding_id=item.finding_id, status="updated"
                        )
                    )
                else:
                    failed += 1
                    results.append(
                        VStrikeFindingResult(
                            finding_id=item.finding_id,
                            status="failed",
                            error="update_finding returned False",
                        )
                    )
                continue

            # Create path: require minimum fields for a useful record
            if item.timestamp is None or item.anomaly_score is None:
                failed += 1
                results.append(
                    VStrikeFindingResult(
                        finding_id=item.finding_id,
                        status="failed",
                        error=(
                            "Finding not found and insufficient fields to "
                            "create (timestamp and anomaly_score required)"
                        ),
                    )
                )
                continue

            new_ctx: dict = dict(item.entity_context_extra or {})
            new_ctx["vstrike"] = enrichment_dict
            finding_data = {
                "finding_id": item.finding_id,
                "timestamp": item.timestamp,
                "anomaly_score": float(item.anomaly_score),
                "data_source": "vstrike",
                "entity_context": new_ctx,
                "severity": item.severity,
                "description": item.description,
                "mitre_predictions": item.mitre_predictions or {},
            }
            if item.predicted_techniques is not None:
                finding_data["predicted_techniques"] = item.predicted_techniques

            created_finding = data_service.create_finding(finding_data)
            if created_finding:
                created += 1
                upserted_ids.append(item.finding_id)
                results.append(
                    VStrikeFindingResult(finding_id=item.finding_id, status="created")
                )
            else:
                failed += 1
                results.append(
                    VStrikeFindingResult(
                        finding_id=item.finding_id,
                        status="failed",
                        error="create_finding returned None",
                    )
                )
        except Exception:
            failed += 1
            logger.exception("VStrike ingest failed for %s", item.finding_id)
            results.append(
                VStrikeFindingResult(
                    finding_id=item.finding_id,
                    status="failed",
                    error=INTERNAL_ERROR_DETAIL,
                )
            )

    case_ids: list[str] = []
    if request.auto_cluster_cases and upserted_ids:
        try:
            from core.cases.case_automation_service import (
                cluster_findings_by_attack_path,
            )

            case_ids = cluster_findings_by_attack_path(upserted_ids)
        except Exception as e:
            logger.exception("VStrike auto-cluster failed: %s", e)

    logger.info(
        "VStrike batch %s: received=%d updated=%d created=%d failed=%d cases=%d",
        request.batch_id,
        len(request.findings),
        updated,
        created,
        failed,
        len(case_ids),
    )

    return VStrikePushResponse(
        batch_id=request.batch_id,
        received=len(request.findings),
        updated=updated,
        created=created,
        failed=failed,
        results=results,
        case_ids=case_ids,
    )


@authenticated_router.get("/health", response_model=VStrikeHealthResponse)
def health_check() -> VStrikeHealthResponse:
    """Check outbound connectivity to the configured VStrike server."""
    service = get_vstrike_service()
    if service is None:
        return VStrikeHealthResponse(
            configured=False,
            reachable=False,
            base_url=None,
            message=(
                "VStrike not configured. Set VSTRIKE_BASE_URL + VSTRIKE_USERNAME + VSTRIKE_PASSWORD "
                "or configure the integration in Settings."
            ),
        )
    success, message = service.test_connection()
    return VStrikeHealthResponse(
        configured=True,
        reachable=success,
        base_url=service.base_url,
        message=message,
    )


@authenticated_router.get("/topology/asset/{asset_id}")
def get_asset_topology(asset_id: str) -> dict:
    """Proxy to VStrike asset-topology lookup (outbound)."""
    service = get_vstrike_service()
    if service is None:
        raise HTTPException(status_code=503, detail="VStrike not configured")
    topology = service.get_asset_topology(asset_id)
    if topology is None:
        raise HTTPException(
            status_code=502,
            detail=f"VStrike did not return topology for asset {asset_id}",
        )
    return {
        "asset_id": asset_id,
        "topology": topology,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


router.include_router(authenticated_router)
