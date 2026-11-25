"""LLM provider management API — CRUD.

See GH issue #88. Each row in `llm_provider_configs` represents a configured
LLM backend (Anthropic, OpenAI, Ollama, ...). API keys are stored in the
secrets_manager under a generated ref name, never in the DB.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth.permissions import deny_unless, permission_gate
from core.llm import residency, statements
from core.llm.bifrost.admin import sync_all_provider_models
from core.llm.providers import provider_service
from core.llm.providers.base_url import validate_base_url_shape
from core.llm.providers.registry import (
    VALID_PROVIDER_TYPES,
    attest_self_hosted,
    invalidate_model_cache,
)
from core.llm.residency import ProviderResidency, Residency
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import delete_secret, secret_write_failure_detail, set_secret
from core.storage.config_service import write_config_audit
from core.storage.models import AIModelConfig, LLMProviderConfig, User
from core.storage.schemas import LLMProviderConfigSchema
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()
router.dependencies.append(permission_gate("platform.read"))

ROUTER_META = RouterMeta(
    prefix="/api/llm/providers",
    tags=["llm-providers"],
    auth=Auth.REQUIRED,
)

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "provider"


def _secret_ref_for(provider_id: str) -> str:
    return f"llm_provider_{provider_id}_api_key"


def _validate_provider_base_url_shape(base_url: Optional[str]) -> None:
    """Shape-only check (see ``validate_base_url_shape``), as a 400."""
    try:
        validate_base_url_shape(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class LLMProviderCreate(BaseModel):
    provider_id: Optional[str] = Field(
        None, description="If omitted, derived from name"
    )
    provider_type: str
    name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    default_model: str
    is_active: bool = True
    is_default: bool = False
    config: Dict[str, Any] = Field(default_factory=dict)


class LLMProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    # Provider Statements, name -> value; written onto every active row of the type.
    statements: Optional[Dict[str, str]] = None


class StatementView(BaseModel):
    value: Optional[str] = None
    state: Literal["held", "pending", "cleared", "absent"]
    by: Optional[str] = None
    by_name: Optional[str] = None
    at: Optional[str] = None
    why: Optional[str] = None


class LLMProviderResponse(BaseModel):
    provider_id: str
    provider_type: str
    name: str
    base_url: Optional[str]
    has_api_key: bool
    default_model: str
    is_active: bool
    is_default: bool
    config: Dict[str, Any]
    last_test_at: Optional[str]
    last_test_success: Optional[bool]
    last_error: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    # Where this provider's calls go; the class only, never the address.
    residency: ProviderResidency
    # The type's Provider Statements, as every row of the type holds them.
    statements: Dict[str, StatementView] = Field(default_factory=dict)


async def _gateway_for(rows: List[LLMProviderConfig]) -> Optional[Dict[str, Residency]]:
    # Read the gateway only when a mirror row needs it.
    if not any(residency.is_gateway_row(r) and not r.base_url for r in rows):
        return {}
    return await residency.gateway_residency()


async def _responses(
    db: UnitOfWorkSession, shown: List[LLMProviderConfig]
) -> List[Dict[str, Any]]:
    """``shown`` as responses, each with its type's statements."""
    types = sorted({r.provider_type for r in shown})
    # The type's active rows; a type with none is read from the row shown.
    by_type = {
        t: statements.rows_of_type(db, t)
        or [next(r for r in shown if r.provider_type == t)]
        for t in types
    }
    every = {r.provider_id: r for rows in by_type.values() for r in rows}
    every.update({r.provider_id: r for r in shown})
    gateway = await _gateway_for(list(every.values()))
    res = {pid: residency.row_residency(r, gateway) for pid, r in every.items()}
    names = statements.account_names(db, statements.authors(every.values()))
    views: Dict[str, Dict[str, Any]] = {}
    for t, rows in by_type.items():
        if statements.unknown(res[r.provider_id] for r in rows):
            views[t] = {}  # nothing to compare a statement with
            continue
        region = await statements.region_for(t)
        views[t] = statements.type_view(rows, res, region, names)
    out = []
    for row in shown:
        d = LLMProviderConfigSchema.dump(row)
        d.pop("api_key_ref", None)
        # The statement records (hashes, counters) are read through ``statements``.
        d["config"] = statements.without_owned(d.get("config"))
        d["residency"] = res[row.provider_id].public()
        d["statements"] = views[row.provider_type]
        out.append(d)
    return out


async def _record_statements(
    db: UnitOfWorkSession,
    user: User,
    row: LLMProviderConfig,
    written: Dict[str, str],
) -> None:
    """Write ``written`` onto every active row of ``row``'s type, ``row`` included.

    Each statement is tied to that row's own address hash. ``row``'s audit entry
    is the update's own; every other row gets its own entry from ``_audit``.
    """
    problem = statements.validate(written)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    rows = statements.rows_of_type(db, row.provider_type, row)
    gateway = await _gateway_for(rows)
    res = {r.provider_id: residency.row_residency(r, gateway) for r in rows}
    why = statements.unknown(res.values())
    if why:
        raise HTTPException(
            status_code=409,
            detail=f"A statement is tied to this provider's address, which isn't known: {why}",
        )
    region = await statements.region_for(row.provider_type)
    by = str(user.user_id)
    for r in rows:
        before = _snapshot(r)
        r.config = statements.stated(r.config, written, by, res[r.provider_id], region)
        if r is not row and _snapshot(r) != before:
            _audit(db, user, r.provider_id, "update", before, _snapshot(r))


_AUDITED_FIELDS = (
    "provider_type",
    "name",
    "base_url",
    "default_model",
    "is_active",
    "is_default",
    "config",
)


def _snapshot(row: Any) -> Dict[str, Any]:
    """What the audit diffs: the row's settings, never its key."""
    snap = {f: getattr(row, f) for f in _AUDITED_FIELDS}
    snap["config"] = statements.audited(snap["config"])
    return snap


def _audit(
    db: UnitOfWorkSession,
    user: User,
    config_key: str,
    action: str,
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    *,
    config_type: str = "llm_provider",
) -> None:
    """One entry on the request's session, written before any key leaves for
    the secrets store or the gateway, so a refused entry stops the change."""
    write_config_audit(
        db,
        config_type=config_type,
        config_key=config_key,
        action=action,
        old_value=before,
        new_value=after,
        changed_by=str(user.user_id),
    )


def _key_change(api_key: Optional[str]) -> Dict[str, str]:
    # Names what happened to the key without its value.
    if api_key is None:
        return {}
    return {"key_change": "cleared" if api_key == "" else "set"}


def _validate_type(provider_type: str) -> None:
    if provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"provider_type must be one of {sorted(VALID_PROVIDER_TYPES)}",
        )


def _refresh_self_hosted_attestation(db: UnitOfWorkSession) -> None:
    """Apply ``config.self_hosted`` to this process's pricing now, not on the
    next refresh. The worker and daemon converge on their own refresh interval."""
    attest_self_hosted(provider_service.active_provider_configs(db))


def _schedule_catalog_resync(reason: str) -> None:
    """Invalidate the model-list cache and fire a background sync.

    Called from provider CRUD so the UI dropdown and Bifrost's allow-list
    reflect the change immediately, rather than waiting up to the next
    scheduled refresh. Best-effort — never blocks the response.
    """
    import asyncio

    invalidate_model_cache()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(sync_all_provider_models())
    logger.info("Model catalog resync scheduled: %s", reason)


@router.get("", response_model=List[LLMProviderResponse])
@router.get("/", response_model=List[LLMProviderResponse])
async def list_providers(
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    return await _responses(db, provider_service.list_providers(db))


@router.post("", response_model=LLMProviderResponse, status_code=201)
@router.post("/", response_model=LLMProviderResponse, status_code=201)
async def create_provider(
    payload: LLMProviderCreate,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    deny_unless(current_user.user_id, "platform.write")
    _validate_type(payload.provider_type)
    _validate_provider_base_url_shape(payload.base_url)

    provider_id = payload.provider_id or _slugify(payload.name)
    if provider_service.get_provider(db, provider_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"provider_id '{provider_id}' already exists"
        )

    created = _snapshot(payload) | _key_change(payload.api_key or None)
    _audit(db, current_user, provider_id, "create", None, created)

    api_key_ref: Optional[str] = None
    if payload.api_key:
        api_key_ref = _secret_ref_for(provider_id)
        if not set_secret(api_key_ref, payload.api_key):
            raise HTTPException(
                status_code=500,
                detail=secret_write_failure_detail("Failed to persist API key"),
            )

    row = LLMProviderConfig(
        provider_id=provider_id,
        provider_type=payload.provider_type,
        name=payload.name,
        base_url=payload.base_url,
        api_key_ref=api_key_ref,
        default_model=payload.default_model,
        is_active=payload.is_active,
        is_default=payload.is_default,
        # Statements are written by their own field on an update, never here.
        config=statements.without_owned(payload.config),
    )
    provider_service.insert(db, row, make_default=payload.is_default)
    if api_key_ref and row.is_active:
        # Live in Bifrost without waiting for a restart: the type's elected
        # row (the default first), with that row's own host.
        provider_service.reconcile_bifrost_key_for_type(db, row.provider_type)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"created provider {provider_id}")
    return (await _responses(db, [row]))[0]


@router.put("/{provider_id}", response_model=LLMProviderResponse)
async def update_provider(
    provider_id: str,
    payload: LLMProviderUpdate,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    deny_unless(current_user.user_id, "platform.write")
    _validate_provider_base_url_shape(payload.base_url)
    row = provider_service.get_provider(db, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    moves_host = payload.base_url is not None and (payload.base_url or None) != (
        row.base_url or None
    )
    if moves_host and row.api_key_ref and payload.api_key is None:
        # A stored key goes only to the host it was entered for.
        raise HTTPException(
            status_code=400,
            detail=(
                "Changing this provider's base URL sends its stored API key to "
                "the new host. Enter the API key again with the new base URL."
            ),
        )

    before = _snapshot(row)
    was_keyed_active = bool(row.api_key_ref) and row.is_active
    if payload.name is not None:
        row.name = payload.name
    if payload.base_url is not None:
        row.base_url = payload.base_url
    if payload.default_model is not None:
        row.default_model = payload.default_model
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.config is not None:
        # The write replaces config, but must not erase the marker that makes a
        # mirror row a mirror row, nor the type's Provider Statements.
        kept = {
            k: v
            for k, v in (row.config or {}).items()
            if k == "managed_by" or k in statements.KEPT_KEYS
        }
        row.config = {**kept, **statements.without_owned(payload.config)}
    if payload.api_key is not None:
        # A credential set or cleared: the type's statements need re-confirming.
        statements.bump_type(db, row.provider_type)
    if payload.statements:
        await _record_statements(db, current_user, row, payload.statements)

    if payload.is_default is True:
        row.is_default = True
        provider_service.clear_other_defaults(db, row.provider_type, provider_id)
    elif payload.is_default is False:
        if row.is_default:
            # Guard: only enforce when transitioning True → False.
            other_default = provider_service.find_other_default(
                db, row.provider_type, provider_id
            )
            if other_default is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cannot unset the only default provider. "
                        "Set another provider as default first."
                    ),
                )
        row.is_default = False

    after = _snapshot(row) | _key_change(payload.api_key)
    if after != before:
        _audit(db, current_user, provider_id, "update", before, after)

    if payload.api_key is not None:
        # Empty string clears the key; non-empty rotates it.
        ref = row.api_key_ref or _secret_ref_for(provider_id)
        if payload.api_key == "":
            delete_secret(ref)
            row.api_key_ref = None
        else:
            if not set_secret(ref, payload.api_key):
                raise HTTPException(
                    status_code=500,
                    detail=secret_write_failure_detail("Failed to persist API key"),
                )
            row.api_key_ref = ref

    provider_service.settle(db, row)
    # Bifrost holds one key and host per type, elected from the active keyed
    # rows; re-elect whenever this row joins, leaves or changes among them.
    is_keyed_active = bool(row.api_key_ref) and row.is_active
    if (was_keyed_active or is_keyed_active) and (
        payload.api_key is not None
        or was_keyed_active != is_keyed_active
        or after.get("is_default") != before.get("is_default")
        or moves_host
    ):
        provider_service.reconcile_bifrost_key_for_type(db, row.provider_type)

    provider_service.settle(db, row)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"updated provider {provider_id}")
    return (await _responses(db, [row]))[0]


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    deny_unless(current_user.user_id, "platform.write")
    row = provider_service.get_provider(db, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    _audit(db, current_user, provider_id, "delete", _snapshot(row), None)
    # The cascade below clears these assignments; record each as its own route does.
    for assigned in db.query(AIModelConfig).filter_by(provider_id=provider_id):
        _audit(
            db,
            current_user,
            assigned.component,
            "delete",
            {"provider_id": provider_id, "model_id": assigned.model_id},
            None,
            config_type="ai_model",
        )

    # Guard: refuse to delete the only active default for this provider type.
    if row.is_default:
        other_active = provider_service.find_other_active(
            db, row.provider_type, provider_id
        )
        if other_active is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete the only active {row.provider_type} provider. "
                    "Add or activate another provider of this type first."
                ),
            )
        provider_service.demote_and_promote(db, row, other_active)

    # Cascade: remove model-config rows that reference this provider before
    # deleting — the FK is ON DELETE RESTRICT so this must happen first.
    provider_service.delete_model_configs_for_provider(db, provider_id)

    if row.api_key_ref:
        try:
            delete_secret(row.api_key_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete secret %s: %s", row.api_key_ref, exc)
        # Bifrost's key is shared per provider_type. Only wipe it if this was
        # the last active keyed provider of its type; otherwise re-push the
        # elected survivor's key, with its host. (``row`` is still in the
        # session here — exclude it explicitly.)
        provider_service.reconcile_bifrost_key_for_type(
            db, row.provider_type, provider_id
        )

    provider_service.remove(db, row)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"deleted provider {provider_id}")
    return {"success": True, "provider_id": provider_id}
