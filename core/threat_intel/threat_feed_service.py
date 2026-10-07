"""STIX/TAXII threat-feed ingestion (Cloudforce One et al).

The poller in `daemon/threat_feed_poller.py` calls into this module on its
configured interval. Imports of `taxii2-client` / `stix2` are deferred so a
missing wheel does not break daemon startup and degrades to a logged no-op.
A TAXII fetch that fails raises, so the poller leaves that collection's
watermark where it is.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.memory.entity_keys import entity_key, text_entity_keys
from core.memory.recall_contract import ENTITY_KEY_TYPES
from core.time import utcnow

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


# ---------------------------------------------------------------------------
# Operator-dropped reports: STIX bundle or plain text -> Entity Keys + T-IDs
# ---------------------------------------------------------------------------

# The same shape core/detections/tools.py scans rules with.
_TECHNIQUE_ID = re.compile(r"\bT\d{4}(?:\.\d{3})?\b", re.ASCII)

# `_STIX_TO_VIGIL_TYPE` speaks the threat_indicators vocabulary, which splits
# hashes by algorithm; Entity Keys have one `hash` type. `entity_key()` does not
# check the type, so an unmapped `hash_md5:...` would mint a key no Verdict
# ever matches.
_INDICATOR_TO_ENTITY_TYPE: Dict[str, str] = {
    "ip": "ip",
    "domain": "domain",
    "url": "url",
    "email": "email",
    "hash_md5": "hash",
    "hash_sha1": "hash",
    "hash_sha256": "hash",
}
_UNMAPPED = set(_STIX_TO_VIGIL_TYPE.values()) - set(_INDICATOR_TO_ENTITY_TYPE)
_OFF_VOCABULARY = set(_INDICATOR_TO_ENTITY_TYPE.values()) - set(ENTITY_KEY_TYPES)
if _UNMAPPED or _OFF_VOCABULARY:
    raise ImportError(
        f"STIX indicator types {sorted(_UNMAPPED)} map onto no Entity Key type and "
        f"{sorted(_OFF_VOCABULARY)} are outside ENTITY_KEY_TYPES"
    )


def _ordered_unique(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _stix_objects(text: str) -> Optional[List[Dict[str, Any]]]:
    """The objects of a STIX bundle, or None when the text is not one.

    A list holding no objects at all (``["seen 8.8.8.8"]``) is not a bundle
    either, and falls through to the text scan like any other JSON.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # deeply nested garbage recurses
        return None
    if isinstance(data, dict):
        data = data.get("objects")
    if not isinstance(data, list):
        return None
    objects = [obj for obj in data if isinstance(obj, dict)]
    return objects or None


def _attack_pattern_ids(obj: Dict[str, Any]) -> Iterable[str]:
    for ref in obj.get("external_references") or []:
        if not isinstance(ref, dict) or ref.get("source_name") != "mitre-attack":
            continue
        external_id = str(ref.get("external_id") or "")
        if _TECHNIQUE_ID.fullmatch(external_id):
            yield external_id


def parse_report(text: str) -> Dict[str, List[str]]:
    """Entity Keys and ATT&CK technique ids from a STIX bundle or a text report.

    A bundle is a JSON list of objects or ``{"objects": [...]}``. Anything else
    — unparseable JSON, JSON of another shape — is scanned as plain text with
    the hunt extractor's patterns. Never raises: empty in, empty lists out.
    """
    if not isinstance(text, str) or not text.strip():
        return {"entity_keys": [], "techniques": []}

    objects = _stix_objects(text)
    if objects is None:
        return {
            "entity_keys": text_entity_keys(text),
            "techniques": _ordered_unique(_TECHNIQUE_ID.findall(text)),
        }

    keys: List[str] = []
    techniques: List[str] = []
    for obj in objects:
        kind = obj.get("type")
        if kind == "indicator":
            for vigil_type, value in _extract_observables(
                str(obj.get("pattern") or "")
            ):
                entity_type = _INDICATOR_TO_ENTITY_TYPE.get(vigil_type)
                if entity_type:
                    keys.append(entity_key(entity_type, value))
        elif kind == "attack-pattern":
            techniques.extend(_attack_pattern_ids(obj))
    return {
        "entity_keys": _ordered_unique(keys),
        "techniques": _ordered_unique(techniques),
    }


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    # Converted before it is made naive: the columns hold UTC, and dropping an
    # offset unconverted moves valid_until by that many hours.
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


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

    A transport, auth, discovery, or missing-collection failure raises. The
    poller already isolates each collection, and a raised error leaves that
    collection's watermark on the last good poll. An empty envelope is a real
    answer and returns []. A missing taxii2-client wheel raises too: it is in
    requirements.txt, so its absence is a broken deploy, not an empty feed.
    """
    from taxii2client.v21 import Server  # type: ignore[import-untyped]

    headers = {"Authorization": f"Bearer {api_token}"}
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
        raise LookupError(
            f"Collection {collection_id} not found on TAXII server {server_url}"
        )
    params: Dict[str, Any] = {}
    if since:
        params["added_after"] = since.isoformat() + "Z"
    envelope = collection.get_objects(**params) if params else collection.get_objects()

    objects = (
        envelope.get("objects")
        if isinstance(envelope, dict)
        else getattr(envelope, "objects", [])
    )
    out: List[NormalizedIndicator] = []
    malformed = 0
    first_error: Optional[Exception] = None
    for obj in objects or []:
        try:
            out.extend(
                parse_stix_indicator(obj, source=source, collection_id=collection_id)
            )
        except Exception as e:  # noqa: BLE001
            malformed += 1
            first_error = first_error or e
    if malformed:
        # Not held against the watermark: a malformed object stays malformed on
        # re-pull, so holding it would re-fetch the same objects every poll.
        logger.warning(
            "Collection %s: %d malformed of %d STIX objects skipped (first: %s)",
            collection_id,
            malformed,
            len(objects or []),
            first_error,
        )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert_indicators(indicators: List[NormalizedIndicator]) -> Dict[str, int]:
    """Upsert normalized indicators into the threat_indicators table.

    Returns inserted/updated/skipped counters. A row that fails is rolled back
    alone and counted in ``skipped``; the other rows still commit.
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
    now = utcnow()
    first_error: Optional[Tuple[str, Exception]] = None
    with db.session_scope() as session:
        for ind in indicators:
            try:
                # A savepoint per row, flushed inside it: a flush-time error
                # (constraint, oversized value) rolls back only this row instead
                # of poisoning the session and failing the whole commit.
                with session.begin_nested():
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
                    else:
                        existing.last_seen = now
                        if ind.confidence is not None:
                            existing.confidence = ind.confidence
                        if ind.threat_level:
                            existing.threat_level = ind.threat_level
                        if ind.labels:
                            existing.labels = ind.labels
                        # Unconditional: a re-published indicator with no
                        # valid_until no longer expires, and keeping the old
                        # past date would hide a hit the feed still reports.
                        existing.valid_until = ind.valid_until
                        if ind.raw_stix:
                            existing.raw_stix = ind.raw_stix
                if existing is None:
                    inserted += 1
                else:
                    updated += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Failed to upsert indicator %s/%s: %s",
                    ind.indicator_type,
                    ind.indicator_value,
                    e,
                )
                first_error = first_error or (
                    f"{ind.indicator_type}/{ind.indicator_value}",
                    e,
                )
                skipped += 1
    if skipped:
        logger.warning(
            "Failed to upsert %d of %d indicators (first: %s: %s)",
            skipped,
            len(indicators),
            *first_error,
        )
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def _unexpired_clause(now: datetime):
    """Live rows: no expiry, or a window that has not closed."""
    from core.storage.models import ThreatIndicator

    return (ThreatIndicator.valid_until.is_(None)) | (ThreatIndicator.valid_until > now)


def lookup_indicators(
    indicator_type: str, values: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Look up a batch of currently-valid indicator values; keyed by value.

    Expired rows (``valid_until`` in the past) are omitted. A NULL expiry is live.
    """
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
    now = utcnow()
    with db.session_scope() as session:
        rows = (
            session.query(ThreatIndicator)
            .filter(ThreatIndicator.indicator_type == indicator_type)
            .filter(ThreatIndicator.indicator_value.in_(values))
            .filter(_unexpired_clause(now))
            .all()
        )
        for row in rows:
            out.setdefault(row.indicator_value, ThreatIndicatorSchema.dump(row))
    return out


# ---------------------------------------------------------------------------
# Hunt proposals from recent feed rows (#905). Proposes; never starts a hunt.
# ---------------------------------------------------------------------------

# How many recent rows one call classifies. A code constant, not a setting: the
# cap bounds the per-key coverage reads, and a feed poll rarely upserts more.
RECENT_INDICATOR_LIMIT = 200


def _recent_indicators(limit: int) -> List[Dict[str, Any]]:
    """The newest ``threat_indicators`` rows by ``last_seen``, dumped."""
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import ThreatIndicator
        from core.storage.schemas import ThreatIndicatorSchema
    except Exception as e:  # noqa: BLE001
        logger.debug("ThreatIndicator read unavailable: %s", e)
        return []
    db = get_db_manager()
    now = utcnow()
    with db.session_scope() as session:
        rows = (
            session.query(ThreatIndicator)
            .filter(_unexpired_clause(now))
            .order_by(ThreatIndicator.last_seen.desc(), ThreatIndicator.id.desc())
            .limit(limit)
            .all()
        )
        return [ThreatIndicatorSchema.dump(row) for row in rows]


def propose_hunts_from_recent_indicators(
    limit: int = RECENT_INDICATOR_LIMIT,
) -> Dict[str, Any]:
    """Recent feed indicators nobody has hunted, each with an executable proposal.

    The second caller of ``check_coverage`` (epic #886, decision 9): read the
    rows the poller upserted, mint one Entity Key per row, and ask the same
    classifier ``check_hunt_coverage`` uses. Classified per key, not per
    batch -- one in-flight hunt would otherwise make every indicator
    ``running``. Rows whose key is ``running`` or ``concluded`` are counted and
    omitted; types with no Entity Key type are skipped rather than minted
    off-vocabulary. Nothing here opens a hunt.
    """
    # Deferred: hunt_coverage imports parse_report from this module.
    from core.memory.hunt_coverage import check_coverage

    limit = max(1, min(int(limit), RECENT_INDICATOR_LIMIT))
    rows = _recent_indicators(limit)
    counts = {"read": len(rows), "skipped": 0, "running": 0, "concluded": 0}
    proposals: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        entity_type = _INDICATOR_TO_ENTITY_TYPE.get(str(row.get("indicator_type")))
        value = row.get("indicator_value")
        # entity_key() answers "" for a value that defangs to nothing.
        key = entity_key(entity_type or "", str(value or ""))
        if not key:
            counts["skipped"] += 1
            continue
        if key in seen:  # the same IOC from two feeds is one question
            continue
        seen.add(key)
        coverage = check_coverage(entity_keys=[key])
        status = coverage["status"]
        if status != "uncovered":
            counts[status] += 1
            continue
        proposals.append(
            {
                "entity_key": key,
                "indicator": {
                    "indicator_type": row.get("indicator_type"),
                    "indicator_value": value,
                    "source": row.get("source"),
                    "last_seen": row.get("last_seen"),  # ISO text: dump is json-mode
                },
                "proposal": coverage["proposal"],
                "execute": coverage["execute"],
            }
        )
    return {"limit": limit, "checked": len(seen), **counts, "proposals": proposals}
