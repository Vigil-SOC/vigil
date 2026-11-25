"""Provider Statements: what the operator states about a provider type.

Stored on each ``llm_provider_configs`` row's ``config`` and held per provider
type: a statement counts only when every active row of the type carries it,
the rule ``attest_self_hosted`` already applies to ``config.self_hosted``.

Each statement records who made it (an account id), when, a SHA-256 of the
address set ``core.llm.residency`` resolved for that row, and the row's
credential generation. A different address hash, or for the region a
different Vertex region hash, clears it. A credential set or cleared since
(the generation moved on) leaves it standing but needing a person to
re-confirm. No address, region or secret is ever stored, only hashes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from core.llm.residency import Residency

logger = logging.getLogger(__name__)

NOT_STATED = "not_stated"

# Each statement's list of values. "not stated" is always one of them.
VALUES: Dict[str, tuple[str, ...]] = {
    "self_hosted": (NOT_STATED, "yes", "no"),
    "baa": (NOT_STATED, "signed", "none"),
    "hipaa_account": (NOT_STATED, "on", "off"),
    "region": (NOT_STATED, "us", "eu_eea", "other"),
    "fedramp": (NOT_STATED, "high", "moderate", "low", "none"),
    "transfer_basis": (NOT_STATED, "adequacy", "dpf", "scc", "bcr", "art49", "none"),
}

# How the check names a statement and its value.
NAMES = {
    "self_hosted": "self-hosted",
    "baa": "BAA",
    "hipaa_account": "HIPAA-eligible account mode",
    "region": "processing and support region",
    "fedramp": "FedRAMP authorization",
    "transfer_basis": "EU transfer basis",
}
VALUE_WORDS = {
    "yes": "yes",
    "no": "no",
    "signed": "signed",
    "none": "none",
    "on": "on",
    "off": "off",
    "us": "United States",
    "eu_eea": "EU/EEA",
    "other": "outside the US and the EU/EEA",
    "high": "High",
    "moderate": "Moderate",
    "low": "Low",
    "adequacy": "adequacy decision",
    "dpf": "EU–US Data Privacy Framework",
    "scc": "standard contractual clauses",
    "bcr": "binding corporate rules",
    "art49": "Art. 49 derogation",
}

# Server-owned config keys: a config write never sets them and never drops them.
STATEMENTS_KEY = "statements"
GENERATION_KEY = "credential_generation"
CHANGED_AT_KEY = "credential_changed_at"
OWNED_KEYS = (STATEMENTS_KEY, GENERATION_KEY, CHANGED_AT_KEY)
# Kept across a config write too, but still writable by hand (the pricing flag).
KEPT_KEYS = (*OWNED_KEYS, "self_hosted")

HELD = "held"
PENDING = "pending"  # standing, but a person has to re-confirm it
CLEARED = "cleared"
ABSENT = "absent"

LEGACY_SELF_HOSTED = "marked self-hosted before this release, not confirmed"
WHY_ADDRESS = "the provider's address changed since it was stated"
WHY_REGION = "the Vertex region changed since it was stated"


def audited(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``config`` for the audit trail: each statement's value, author and date,
    without the address and region hashes (their inputs are easy to guess)."""
    if not config or STATEMENTS_KEY not in config:
        return config
    records = {
        name: {k: v for k, v in r.items() if k not in ("addresses", "region")}
        for name, r in (config.get(STATEMENTS_KEY) or {}).items()
        if isinstance(r, dict)
    }
    return {**config, STATEMENTS_KEY: records}


def address_hash(res: Residency) -> str:
    return hashlib.sha256("\n".join(sorted(res.addresses)).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate(statements: Dict[str, str]) -> Optional[str]:
    """Why ``statements`` can't be written, or None."""
    for name, value in statements.items():
        if name not in VALUES:
            return f"Unknown statement {name!r}. Statements: {', '.join(VALUES)}."
        if value not in VALUES[name]:
            return f"{name} must be one of {', '.join(VALUES[name])}."
    return None


def without_owned(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """An incoming config with the server-owned statement keys removed."""
    return {k: v for k, v in (config or {}).items() if k not in OWNED_KEYS}


def generation(config: Optional[Dict[str, Any]]) -> int:
    return int((config or {}).get(GENERATION_KEY) or 0)


def bumped(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``config`` with the credential generation moved on. A new dict."""
    return {
        **(config or {}),
        GENERATION_KEY: generation(config) + 1,
        CHANGED_AT_KEY: _now(),
    }


def stated(
    config: Optional[Dict[str, Any]],
    statements: Dict[str, str],
    by: str,
    res: Residency,
    region: Optional[str],
) -> Dict[str, Any]:
    """``config`` with ``statements`` recorded (or cleared) as ``by``. A new dict.

    Saving a value again re-confirms it: new author, date, hashes and generation.
    """
    config = dict(config or {})
    records = dict(config.get(STATEMENTS_KEY) or {})
    at = _now()
    for name, value in statements.items():
        if value == NOT_STATED:
            records.pop(name, None)
        else:
            record = {
                "value": value,
                "by": by,
                "at": at,
                "addresses": address_hash(res),
                "generation": generation(config),
            }
            if name == "region":
                record["region"] = region
            records[name] = record
        if name == "self_hosted":
            # The pricing flag follows the statement.
            if value == "yes":
                config["self_hosted"] = True
            else:
                config.pop("self_hosted", None)
    if records:
        config[STATEMENTS_KEY] = records
    else:
        config.pop(STATEMENTS_KEY, None)
    return config


def standing(
    config: Optional[Dict[str, Any]],
    name: str,
    res: Residency,
    region: Optional[str],
) -> Dict[str, Any]:
    """One row's statement: its value, state, author, date and why."""
    config = config or {}
    record = (config.get(STATEMENTS_KEY) or {}).get(name)
    if not isinstance(record, dict) or record.get("value") in (None, NOT_STATED):
        if name == "self_hosted" and config.get("self_hosted") is True:
            # A flag written before statements existed still prices; it isn't counted.
            return {"value": "yes", "state": PENDING, "why": LEGACY_SELF_HOSTED}
        return {"value": None, "state": ABSENT}
    out = {"value": record["value"], "by": record.get("by"), "at": record.get("at")}
    if (
        name == "self_hosted"
        and record["value"] == "yes"
        and config.get("self_hosted") is not True
    ):
        return {**out, "value": None, "state": ABSENT}  # flag cleared by hand
    if record.get("addresses") != address_hash(res):
        return {**out, "state": CLEARED, "why": WHY_ADDRESS}
    if name == "region" and record.get("region") != region:
        return {**out, "state": CLEARED, "why": WHY_REGION}
    if int(record.get("generation") or 0) < generation(config):
        changed = str(config.get(CHANGED_AT_KEY) or "")[:10]
        why = (
            f"made before the credential changed on {changed}"
            if changed
            else ("made before the credential changed")
        )
        return {**out, "state": PENDING, "why": why}
    return {**out, "state": HELD}


_RANK = {ABSENT: 0, CLEARED: 0, PENDING: 1, HELD: 2}


def type_view(
    rows: Iterable[Any],
    residencies: Dict[str, Residency],
    region: Optional[str],
    names: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Each statement as the console shows it for one provider type.

    Per type: the weakest row decides the state, and a value counts only when
    every row states the same one. ``names`` maps an account id to its name.
    """
    rows = list(rows)
    view: Dict[str, Dict[str, Any]] = {}
    for name in VALUES:
        each = [
            standing(r.config, name, residencies[r.provider_id], region) for r in rows
        ]
        if not each:
            continue
        worst = min(each, key=lambda s: _RANK[s["state"]])
        values = {s.get("value") for s in each}
        item = dict(worst)
        if len(values) > 1:
            item = {
                "value": None,
                "state": ABSENT,
                "why": "the rows of this type disagree",
            }
        if item.get("by"):
            item["by_name"] = names.get(item["by"]) or "a deleted account"
        view[name] = item
    return view


def authors(rows: Iterable[Any]) -> List[str]:
    """Every account id that made a statement on ``rows``."""
    ids = set()
    for r in rows:
        for record in ((r.config or {}).get(STATEMENTS_KEY) or {}).values():
            if isinstance(record, dict) and record.get("by"):
                ids.add(str(record["by"]))
    return sorted(ids)


def account_names(session: Any, ids: Iterable[str]) -> Dict[str, str]:
    from core.storage.models import User

    ids = list(ids)
    if not ids:
        return {}
    rows = session.query(User.user_id, User.full_name, User.username).filter(
        User.user_id.in_(ids)
    )
    return {r.user_id: r.full_name or r.username for r in rows}


def rows_of_type(session: Any, provider_type: str, also: Any = None) -> List[Any]:
    """Every active row of ``provider_type``, plus ``also`` when it is inactive."""
    from core.storage.models import LLMProviderConfig

    rows = (
        session.query(LLMProviderConfig)
        .filter(
            LLMProviderConfig.provider_type == provider_type,
            LLMProviderConfig.is_active.is_(True),
        )
        .order_by(LLMProviderConfig.created_at)
        .all()
    )
    if also is not None and all(r.provider_id != also.provider_id for r in rows):
        rows.append(also)
    return rows


def bump_type(session: Any, provider_type: str) -> None:
    """A credential of ``provider_type`` was set or cleared: every row's
    statements now need a person to re-confirm them. Inactive rows too, so a
    row brought back later doesn't read as held."""
    from core.storage.models import LLMProviderConfig

    for row in session.query(LLMProviderConfig).filter(
        LLMProviderConfig.provider_type == provider_type
    ):
        row.config = bumped(row.config)


def bump_type_now(provider_type: str) -> None:
    """``bump_type`` in its own transaction, for the gateway proxy."""
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        bump_type(session, provider_type)


def unknown(residencies: Iterable[Residency]) -> Optional[str]:
    """The reason the first row whose address isn't known gives, else None.

    A cloud-account type keeps its class with no address, so the test is
    ``address_known``, not the class.
    """
    for res in residencies:
        if not res.address_known:
            return res.reason
    return None


async def vertex_region_hash() -> Optional[str]:
    """SHA-256 of the regions in Vigil's own copy of each Vertex key's scope.

    Key ids come from the routability snapshot; the regions only from the
    secrets store (``refs.scope_ref``), since the gateway masks them. None when
    the gateway can't be read.
    """
    from core.llm.bifrost import path_health
    from core.llm.bifrost.refs import scope_ref
    from core.secrets import get_secret

    snap = await path_health.read()
    if snap["routability_error"] is not None:
        return None
    keys = (snap["routability"] or {}).get("keys") or {}
    regions = []
    for key_id, verdict in keys.items():
        if (verdict or {}).get("provider") != "vertex":
            continue
        try:
            scope = json.loads(get_secret(scope_ref(key_id)) or "{}")
        except ValueError:
            scope = {}
        regions.append(str(scope.get("region") or ""))
    return hashlib.sha256("\n".join(sorted(regions)).encode()).hexdigest()


async def region_for(provider_type: str) -> Optional[str]:
    """The region hash a statement of ``provider_type`` is tied to: Vertex only."""
    return await vertex_region_hash() if provider_type == "vertex" else None
