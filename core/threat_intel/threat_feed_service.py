"""STIX/TAXII threat-feed ingestion (Cloudforce One et al).

The poller in `daemon/threat_feed_poller.py` calls into this module on its
configured interval. Imports of `taxii2-client` / `stix2` are deferred so a
missing wheel does not break daemon startup; failures degrade to a logged
no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Map STIX 2.1 indicator pattern prefixes to our normalized indicator_type.
_STIX_TO_VIGIL_TYPE: Dict[str, str] = {
    "ipv4-addr:value": "ip",
    "ipv6-addr:value": "ip",
    "domain-name:value": "domain",
    "url:value": "url",
    "file:hashes.MD5": "hash_md5",
    "file:hashes.'SHA-1'": "hash_sha1",
    "file:hashes.'SHA-256'": "hash_sha256",
    "email-addr:value": "email",
}


@dataclass
class NormalizedIndicator:
    indicator_type: str
    indicator_value: str
    source: str
    collection_id: Optional[str]
    confidence: Optional[float]
    threat_level: Optional[str]
    labels: List[str]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    raw_stix: Dict[str, Any]


def parse_stix_indicator(
    obj: Dict[str, Any], source: str, collection_id: Optional[str]
) -> List[NormalizedIndicator]:
    """Parse a STIX 2.1 ``indicator`` SDO into zero or more NormalizedIndicators.

    A single STIX pattern can contain multiple atomic observables (e.g. an OR
    of several IPs); we emit one NormalizedIndicator per atomic match.
    """
    if obj.get("type") != "indicator":
        return []

    pattern = obj.get("pattern", "")
    if not pattern:
        return []

    out: List[NormalizedIndicator] = []
    confidence = obj.get("confidence")
    threat_level = _confidence_to_level(confidence)
    labels = list(obj.get("labels") or [])
    valid_from = _parse_dt(obj.get("valid_from"))
    valid_until = _parse_dt(obj.get("valid_until"))

    for vigil_type, value in _extract_observables(pattern):
        out.append(
            NormalizedIndicator(
                indicator_type=vigil_type,
                indicator_value=value,
                source=source,
                collection_id=collection_id,
                confidence=float(confidence) if confidence is not None else None,
                threat_level=threat_level,
                labels=labels,
                valid_from=valid_from,
                valid_until=valid_until,
                raw_stix=obj,
            )
        )
    return out


def _extract_observables(pattern: str) -> Iterable[Tuple[str, str]]:
    """Best-effort extraction of (vigil_type, value) pairs from a STIX 2.1 pattern.

    Handles the common shapes Cloudforce One emits: simple equality
    (``[ipv4-addr:value = '1.2.3.4']``) and OR'd lists. Anything more exotic
    is logged at debug and skipped — we are explicitly OK with feed-side
    quirks producing fewer indicators rather than crashing.
    """
    # Strip the brackets and split on " OR " (STIX 2.1 logical operator).
    body = pattern.strip()
    if body.startswith("["):
        body = body[1:]
    if body.endswith("]"):
        body = body[:-1]

    for clause in [c.strip() for c in body.split(" OR ")]:
        if "=" not in clause:
            continue
        key, _, raw_value = clause.partition("=")
        key = key.strip()
        value = raw_value.strip().strip("'").strip('"')
        if not value:
            continue
        vigil_type = _STIX_TO_VIGIL_TYPE.get(key)
        if not vigil_type:
            logger.debug("Skipping unrecognized STIX pattern key: %s", key)
            continue
        yield vigil_type, value


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None
    return None


def _confidence_to_level(confidence: Optional[Any]) -> Optional[str]:
    if confidence is None:
        return None
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return None
    if c >= 90:
        return "critical"
    if c >= 75:
        return "high"
    if c >= 50:
        return "medium"
    if c >= 25:
        return "low"
    return "info"


# ---------------------------------------------------------------------------
# TAXII 2.1 fetch
# ---------------------------------------------------------------------------


def fetch_taxii_collection(
    server_url: str,
    collection_id: str,
    api_token: str,
    source: str,
    since: Optional[datetime] = None,
) -> List[NormalizedIndicator]:
    """Pull a single TAXII 2.1 collection and return normalized indicators.

    Returns an empty list (not an exception) on failure so the poller can
    keep going across other collections.
    """
    try:
        from taxii2client.v21 import Server  # type: ignore[import-untyped]
    except Exception as e:  # noqa: BLE001
        logger.warning("taxii2-client not available, skipping fetch: %s", e)
        return []

    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        server = Server(server_url, headers=headers)
        collection = None
        for api_root in server.api_roots:
            for c in api_root.collections:
                if c.id == collection_id:
                    collection = c
                    break
            if collection:
                break
        if collection is None:
            logger.warning(
                "Collection %s not found on TAXII server %s", collection_id, server_url
            )
            return []
        params: Dict[str, Any] = {}
        if since:
            params["added_after"] = since.isoformat() + "Z"
        envelope = (
            collection.get_objects(**params) if params else collection.get_objects()
        )
    except Exception as e:  # noqa: BLE001
        logger.error("TAXII fetch failed (%s/%s): %s", server_url, collection_id, e)
        return []

    objects = (
        envelope.get("objects")
        if isinstance(envelope, dict)
        else getattr(envelope, "objects", [])
    )
    out: List[NormalizedIndicator] = []
    for obj in objects or []:
        try:
            out.extend(
                parse_stix_indicator(obj, source=source, collection_id=collection_id)
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Skipping malformed STIX object: %s", e)
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert_indicators(indicators: List[NormalizedIndicator]) -> Dict[str, int]:
    """Upsert normalized indicators into the threat_indicators table.

    Returns counters for inserted/updated rows.
    """
    if not indicators:
        return {"inserted": 0, "updated": 0, "skipped": 0}

    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import ThreatIndicator
    except Exception as e:  # noqa: BLE001
        logger.error("Cannot import DB manager / ThreatIndicator: %s", e)
        return {"inserted": 0, "updated": 0, "skipped": len(indicators)}

    db = get_db_manager()
    inserted = 0
    updated = 0
    skipped = 0
    now = datetime.utcnow()
    with db.session_scope() as session:
        for ind in indicators:
            try:
                existing = (
                    session.query(ThreatIndicator)
                    .filter_by(
                        source=ind.source,
                        indicator_type=ind.indicator_type,
                        indicator_value=ind.indicator_value,
                    )
                    .one_or_none()
                )
                if existing is None:
                    session.add(
                        ThreatIndicator(
                            indicator_type=ind.indicator_type,
                            indicator_value=ind.indicator_value,
                            source=ind.source,
                            collection_id=ind.collection_id,
                            confidence=ind.confidence,
                            threat_level=ind.threat_level,
                            labels=ind.labels,
                            valid_from=ind.valid_from,
                            valid_until=ind.valid_until,
                            raw_stix=ind.raw_stix,
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    inserted += 1
                else:
                    existing.last_seen = now
                    if ind.confidence is not None:
                        existing.confidence = ind.confidence
                    if ind.threat_level:
                        existing.threat_level = ind.threat_level
                    if ind.labels:
                        existing.labels = ind.labels
                    if ind.valid_until:
                        existing.valid_until = ind.valid_until
                    if ind.raw_stix:
                        existing.raw_stix = ind.raw_stix
                    updated += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Failed to upsert indicator %s/%s: %s",
                    ind.indicator_type,
                    ind.indicator_value,
                    e,
                )
                skipped += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def lookup_indicators(
    indicator_type: str, values: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Look up a batch of indicator values; return matches keyed by value."""
    if not values:
        return {}
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import ThreatIndicator
        from core.storage.schemas import ThreatIndicatorSchema
    except Exception as e:  # noqa: BLE001
        logger.debug("ThreatIndicator lookup unavailable: %s", e)
        return {}
    db = get_db_manager()
    out: Dict[str, Dict[str, Any]] = {}
    with db.session_scope() as session:
        rows = (
            session.query(ThreatIndicator)
            .filter(ThreatIndicator.indicator_type == indicator_type)
            .filter(ThreatIndicator.indicator_value.in_(values))
            .all()
        )
        for row in rows:
            out.setdefault(row.indicator_value, ThreatIndicatorSchema.dump(row))
    return out
